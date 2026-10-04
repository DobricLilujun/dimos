# Copyright 2026 Dimensional Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""SEDAN GROUP robot console: a web control deck for a robot stack.

The console is a :class:`Module` that adds a web UI on top of a running stack
without disturbing the existing Rerun viewer (which it embeds by iframe, so all
original Rerun content is preserved). It provides:

* a control deck of buttons for the workflow operations described in the
  ``unitree-go2-agentic-persistent`` usage tutorial (alignment, map capture,
  object/location tagging, memory query, navigation, stop),
* a ChatGPT-style chat that sends text to the agent over the ``/human_input``
  channel and renders the agent's replies (``/agent``) plus the internal tool
  input/output (``/agent`` tool messages + ``/tool_streams`` notifications),
* live status badges (alignment, navigation readiness, agent idle).

The module talks to the rest of the stack the same way the MCP server does:

* ``@skill`` methods (tagging / query / navigation) are invoked over the MCP
  server's HTTP endpoint (``tools/call``).
* ``@rpc``-only human actions (alignment confirm/reject, save/finish capture)
  are invoked directly over the RPC backend by ``<ModuleClass>/<method>``.
* chat + tool I/O are carried over the pub/sub transport (``make_transport``).

Nothing here declares ``In``/``Out`` ports, so the console never competes with
the stack's stream wiring; it only observes/publishes by channel name.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any, cast

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from starlette.responses import StreamingResponse

from dimos.core.core import rpc
from dimos.core.global_config import global_config
from dimos.core.module import Module, ModuleConfig
from dimos.utils.logging_config import setup_logger

if TYPE_CHECKING:
    from dimos.core.transport import PubSubTransport

logger = setup_logger()

# The module class whose ``@rpc``-only human actions the console drives. The
# alignment flow lives on PersistentGo2Map; callers can override via config.
DEFAULT_MAP_MODULE = "PersistentGo2Map"


# ---------------------------------------------------------------------------
# Operation registry
# ---------------------------------------------------------------------------
# Every control-deck button maps to one of these. ``kind`` selects how the
# console invokes the operation:
#
#   * ``"rpc"``  -- an ``@rpc``-only human action, called over the RPC backend
#                   by ``<module>/<method>`` (never exposed to the LLM agent).
#   * ``"mcp"``  -- an ``@skill`` method, called over the MCP server HTTP API
#                   (the same tools the agent can call).
#
# ``human_only`` marks actions that must stay human-triggered (e.g. alignment
# confirmation) so the front-end renders them with a confirm step.
# ``group`` / ``label`` / ``schema`` drive the control-deck layout.


class Operation:
    def __init__(
        self,
        key: str,
        label: str,
        kind: str,
        group: str,
        *,
        method: str | None = None,
        module: str = DEFAULT_MAP_MODULE,
        human_only: bool = False,
        schema: dict[str, Any] | None = None,
        description: str = "",
        primary: bool = False,
    ) -> None:
        self.key = key
        self.label = label
        self.kind = kind
        self.group = group
        self.method = method or key
        self.module = module
        self.human_only = human_only
        self.schema = schema or {}
        self.description = description
        self.primary = primary

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "group": self.group,
            "human_only": self.human_only,
            "schema": self.schema,
            "description": self.description,
            "primary": self.primary,
        }


# Order matters: it is the control-deck layout.
OPERATIONS: dict[str, Operation] = {
    # ---- Alignment (reconnect state alignment) ----
    "alignment_status": Operation(
        "alignment_status",
        "Alignment status",
        "rpc",
        "Alignment",
        description="Query the current alignment candidate and progress.",
    ),
    "navigation_ready": Operation(
        "navigation_ready",
        "Navigation ready?",
        "rpc",
        "Alignment",
        description="Whether navigation is enabled (post-alignment).",
    ),
    "confirm_alignment": Operation(
        "confirm_alignment",
        "Confirm alignment",
        "rpc",
        "Alignment",
        human_only=True,
        primary=True,
        description="Human-only: accept the current alignment candidate.",
    ),
    "reject_alignment": Operation(
        "reject_alignment",
        "Reject alignment",
        "rpc",
        "Alignment",
        human_only=True,
        description="Human-only: discard the current alignment candidate.",
    ),
    # ---- Map capture ----
    "save_map": Operation(
        "save_map",
        "Save map",
        "rpc",
        "Map",
        human_only=True,
        description="Atomically persist the current map to disk.",
    ),
    "finish_startup_capture": Operation(
        "finish_startup_capture",
        "Finish startup capture",
        "rpc",
        "Map",
        human_only=True,
        description="Human-only: finalize the manual/rotating startup capture.",
    ),
    "cancel_startup_rotation": Operation(
        "cancel_startup_rotation",
        "Cancel rotation",
        "rpc",
        "Map",
        human_only=True,
        description="Human-only: cancel the startup rotation sweep.",
    ),
    # ---- Memory tagging (agent skills, also callable by the LLM) ----
    "tag_object": Operation(
        "tag_object",
        "Tag object",
        "mcp",
        "Tagging",
        primary=True,
        description="Tag the object the robot is looking at.",
        schema={
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "Short label, e.g. 'coffee table'"},
                "object_type": {"type": "string", "description": "Category, e.g. 'furniture'"},
                "notes": {"type": "string", "description": "Optional note"},
            },
            "required": ["tag"],
        },
    ),
    "tag_location": Operation(
        "tag_location",
        "Tag location",
        "mcp",
        "Tagging",
        description="Tag the current robot position.",
        schema={
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "Short label, e.g. 'front door'"},
                "location_type": {"type": "string", "description": "Category, e.g. 'entry'"},
                "notes": {"type": "string", "description": "Optional note"},
            },
            "required": ["tag"],
        },
    ),
    "query_memory_tags": Operation(
        "query_memory_tags",
        "Query memory",
        "mcp",
        "Memory",
        description="Search saved tags (objects and locations).",
        schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text query"},
                "tag_type": {"type": "string", "description": "'object' | 'location' | '' (all)"},
            },
        },
    ),
    # ---- Navigation (agent skills) ----
    "navigate_to_memory_tag": Operation(
        "navigate_to_memory_tag",
        "Navigate to tag",
        "mcp",
        "Navigation",
        primary=True,
        description="Navigate to a saved location tag.",
        schema={
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "Location tag to navigate to"},
                "notes": {"type": "string", "description": "Optional note"},
            },
            "required": ["tag"],
        },
    ),
    "stop_navigation": Operation(
        "stop_navigation",
        "Stop navigation",
        "mcp",
        "Navigation",
        description="Stop the current navigation run.",
    ),
}


# ---------------------------------------------------------------------------
# Module
# ---------------------------------------------------------------------------
class RobotConsoleModuleConfig(ModuleConfig):
    port: int = 8090
    mcp_port: int = 9990
    rerun_web_port: int = 9878
    map_module: str = DEFAULT_MAP_MODULE


class RobotConsoleModule(Module):
    """A web control deck + ChatGPT-style chat for a robot stack."""

    config: RobotConsoleModuleConfig

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # event bus: transport-thread -> console-server-thread
        self._event_queue: asyncio.Queue[Any] | None = None
        self._console_loop: asyncio.AbstractEventLoop | None = None
        self._server: Any = None
        self._server_thread: threading.Thread | None = None
        self._agent_transport: PubSubTransport[Any] | None = None
        self._idle_transport: PubSubTransport[Any] | None = None
        self._tool_transport: PubSubTransport[Any] | None = None
        self._status: dict[str, Any] = {"agent_idle": False}
        self._clients: set[asyncio.Queue[Any]] = set()

    # -- lifecycle --------------------------------------------------------
    @property
    def mcp_url(self) -> str:
        return f"http://127.0.0.1:{self.config.mcp_port}/mcp"

    @property
    def rerun_url(self) -> str:
        return f"http://127.0.0.1:{self.config.rerun_web_port}"

    @rpc
    def start(self) -> None:
        super().start()
        self._setup_agent_streams()
        self._start_server()

    @rpc
    def stop(self) -> None:
        super().stop()
        self._stop_server()
        self._teardown_agent_streams()

    def _stop_server(self) -> None:
        """Shut the uvicorn server down and join its worker thread."""
        server = self._server
        if server is not None:
            try:
                server.should_exit = True
            except Exception:
                logger.debug("console: server.should_exit set failed")
        thread = self._server_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._server = None
        self._server_thread = None

    # -- agent / tool streams --------------------------------------------
    def _setup_agent_streams(self) -> None:
        """Subscribe to the agent / idle / tool-stream channels by name."""
        try:
            from dimos.core.transport_factory import make_transport

            self._agent_transport = make_transport("/agent")
            self._agent_transport.start()
            self._agent_transport.subscribe(self._on_agent)

            self._idle_transport = make_transport("/agent_idle")
            self._idle_transport.start()
            self._idle_transport.subscribe(self._on_idle)

            self._tool_transport = make_transport("/tool_streams")
            self._tool_transport.start()
            self._tool_transport.subscribe(self._on_tool)
        except Exception:
            logger.exception("console: failed to set up agent streams")

    def _teardown_agent_streams(self) -> None:
        for transport in (
            self._agent_transport,
            self._idle_transport,
            self._tool_transport,
        ):
            if transport is None:
                continue
            try:
                transport.stop()
            except Exception:
                logger.exception("console: transport stop failed")

    def _emit(self, event: dict[str, Any]) -> None:
        """Thread-safe: fan an event out to every connected SSE client."""
        loop = self._console_loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._broadcast, event)
        except Exception:
            logger.debug("console: drop event %s", event.get("type"))

    def _broadcast(self, event: dict[str, Any]) -> None:
        for client_queue in self._clients:
            try:
                client_queue.put_nowait(event)
            except Exception:
                pass

    def _on_agent(self, message: Any) -> None:
        payload = serialize_message(message)
        if payload is None:
            return
        self._status["agent_idle"] = False
        self._emit({"type": "message", **payload})
        self._emit({"type": "status", "agent_idle": False, **self._status})

    def _on_idle(self, idle: Any) -> None:
        self._status["agent_idle"] = bool(idle)
        self._emit({"type": "status", "agent_idle": bool(idle), **self._status})

    def _on_tool(self, frame: Any) -> None:
        if not isinstance(frame, dict):
            return
        method = frame.get("method")
        if method == "notifications/message":
            params = frame.get("params", {})
            self._emit(
                {
                    "type": "tool",
                    "name": params.get("logger", ""),
                    "text": params.get("data", ""),
                }
            )
        elif method == "notifications/progress":
            params = frame.get("params", {})
            self._emit(
                {
                    "type": "tool",
                    "name": params.get("_meta", {}).get("tool_name", ""),
                    "text": params.get("message", ""),
                    "progress": params.get("progress"),
                    "total": params.get("total"),
                }
            )

    # -- server -----------------------------------------------------------
    def _start_server(self) -> None:
        app = self._build_app()
        config = uvicorn.Config(
            app,
            host=global_config.listen_host or "127.0.0.1",
            port=self.config.port,
            log_level="warning",
        )
        self._server = uvicorn.Server(config)

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._console_loop = loop
            self._event_queue = asyncio.Queue(maxsize=4096)
            try:
                loop.run_until_complete(self._server.serve())
            except Exception:
                logger.exception("console: server failed")
            finally:
                try:
                    loop.run_until_complete(self._server.shutdown())
                except Exception:
                    pass
                self._console_loop = None
                loop.close()

        self._server_thread = threading.Thread(
            target=_run, name="robot-console-uvicorn", daemon=True
        )
        self._server_thread.start()
        logger.info("console: web control deck on http://127.0.0.1:%d", self.config.port)

    # -- app / endpoints --------------------------------------------------
    def _build_app(self) -> FastAPI:
        from .frontend import INDEX_HTML

        app = FastAPI(title="SEDAN GROUP Robot Console")
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @app.get("/")
        def index() -> Response:
            return Response(INDEX_HTML, media_type="text/html; charset=utf-8")

        @app.get("/api/config")
        def api_config() -> dict[str, Any]:
            return {
                "rerun_url": self.rerun_url,
                "map_module": self.config.map_module,
                "mcp_url": self.mcp_url,
                "operations": [OPERATIONS[k].to_dict() for k in OPERATIONS],
            }

        @app.get("/api/status")
        def api_status() -> dict[str, Any]:
            return self._status

        @app.get("/api/tools")
        def api_tools() -> list[dict[str, Any]]:
            return [OPERATIONS[k].to_dict() for k in OPERATIONS]

        @app.post("/api/action")
        async def api_action(payload: dict[str, Any]) -> dict[str, Any]:
            key = payload.get("name") or payload.get("key")
            args = payload.get("args") or {}
            if key not in OPERATIONS:
                return {"ok": False, "error": f"unknown operation: {key!r}"}
            return await self._dispatch(key, args)

        @app.post("/api/chat")
        async def api_chat(payload: dict[str, Any]) -> dict[str, Any]:
            text = (payload.get("message") or payload.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": "empty message"}
            self._publish_human_input(text)
            return {"ok": True, "echo": text}

        @app.get("/api/refresh-status")
        async def api_refresh_status() -> dict[str, Any]:
            return await self._refresh_status()

        # dispatch / transport helpers (defined after _build_app)

        @app.get("/events")
        async def events() -> StreamingResponse:
            client_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=4096)
            self._clients.add(client_queue)

            async def generator() -> Any:
                yield _sse("status", self._status)
                try:
                    while True:
                        try:
                            event = await asyncio.wait_for(client_queue.get(), timeout=15.0)
                            yield _sse(event.get("type", "event"), event)
                        except asyncio.TimeoutError:
                            yield ": ping\n\n"  # keep-alive
                finally:
                    self._clients.discard(client_queue)

            return StreamingResponse(
                generator(), media_type="text/event-stream"
            )

        return app

    # -- dispatch ---------------------------------------------------------
    async def _dispatch(self, key: str, args: dict[str, Any]) -> dict[str, Any]:
        op = OPERATIONS[key]
        if op.kind == "rpc":
            return await self._call_rpc(op, args)
        if op.kind == "mcp":
            return await self._call_mcp(op, args)
        return {"ok": False, "error": f"unknown kind for {key}"}

    async def _call_rpc(self, op: Operation, args: dict[str, Any]) -> dict[str, Any]:
        address = f"{self.config.map_module}/{op.method}"
        return await self._run_blocking(_rpc_call, self.rpc, address, args)

    async def _call_mcp(self, op: Operation, args: dict[str, Any]) -> dict[str, Any]:
        import httpx

        request = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tools/call",
            "params": {"name": op.key, "arguments": args},
        }
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(self.mcp_url, json=request)
                data = response.json()
        except Exception as exc:
            return {"ok": False, "error": f"mcp request failed: {exc}"}

        if "error" in data:
            return {"ok": False, "error": _format_mcp_error(data["error"])}
        result = data.get("result", data)
        content = ""
        if isinstance(result, dict):
            content = result.get("content", "")
            if isinstance(content, list):
                content = "".join(
                    c.get("text", "") for c in content if isinstance(c, dict)
                )
        return {"ok": True, "result": _coerce(content)}

    async def _run_blocking(self, func: Any, *args: Any) -> dict[str, Any]:
        """Run a blocking RPC call on a dedicated pool (keeps the loop free)."""
        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            result = await loop.run_in_executor(pool, lambda: func(*args))
        return cast(dict[str, Any], result)

    # -- chat / status ----------------------------------------------------
    def _publish_human_input(self, text: str) -> None:
        try:
            from dimos.core.transport_factory import make_transport

            transport = make_transport("/human_input")
            transport.start()
            transport.publish(text)
            transport.stop()
        except Exception:
            logger.exception("console: failed to publish human_input")

    async def _refresh_status(self) -> dict[str, Any]:
        return await self._run_blocking(_status_probe, self.rpc, self.config.map_module, self._status)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _rpc_call(
    rpc: Any, address: str, args: dict[str, Any]
) -> dict[str, Any]:
    try:
        result, unsub = rpc.call_sync(address, ([], args), rpc_timeout=120.0)
        try:
            unsub()
        except Exception:
            pass
        return {"ok": True, "result": _coerce(result)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _status_probe(
    rpc: Any, map_module: str, base: dict[str, Any]
) -> dict[str, Any]:
    status: dict[str, Any] = dict(base)
    status["timestamp"] = time.time()
    try:
        for method in ("alignment_status", "navigation_ready"):
            address = f"{map_module}/{method}"
            try:
                result, unsub = rpc.call_sync(address, ([], {}), rpc_timeout=10.0)
                try:
                    unsub()
                except Exception:
                    pass
                status[method] = _coerce(result)
            except Exception as exc:
                status[method] = f"error: {exc}"
    except Exception as exc:
        status["error"] = str(exc)
    return status


def serialize_message(message: Any) -> dict[str, Any] | None:
    """Serialize a langchain BaseMessage (or dict) to a chat-safe payload."""
    if message is None:
        return None
    if isinstance(message, dict):
        return _normalize_message(message)
    try:
        from langchain_core.messages import (
            AIMessage,
            HumanMessage,
            SystemMessage,
            ToolMessage,
        )
    except Exception:
        return _normalize_message(_obj_to_dict(message))

    if isinstance(message, HumanMessage):
        role = "user"
    elif isinstance(message, AIMessage):
        role = "agent"
    elif isinstance(message, ToolMessage):
        role = "tool"
    elif isinstance(message, SystemMessage):
        role = "system"
    else:
        role = getattr(message, "type", "agent") or "agent"
    return _normalize_message(
        {
            "role": role,
            "content": _content_to_text(getattr(message, "content", "")),
            "tool_calls": getattr(message, "tool_calls", None),
            "tool_call_id": getattr(message, "tool_call_id", None),
            "name": getattr(message, "name", None),
            "metadata": getattr(message, "metadata", None),
        }
    )


def _obj_to_dict(obj: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(obj.json())
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return {
        "role": "agent",
        "content": _content_to_text(getattr(obj, "content", "")),
        "tool_calls": getattr(obj, "tool_calls", None),
    }


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "\n".join(part for part in parts if part)
    return str(content) if content else ""


def _normalize_message(raw: dict[str, Any]) -> dict[str, Any]:
    """Build a chat payload with a stable shape for the front-end."""
    role = raw.get("role") or "agent"
    content = _content_to_text(raw.get("content", ""))
    tool_calls = raw.get("tool_calls") or []
    tool_call_id = raw.get("tool_call_id")
    # Tool messages carry the result; expose name + text for the UI.
    if role == "tool":
        return {
            "kind": "tool",
            "role": "tool",
            "name": raw.get("name") or raw.get("tool_name") or "tool",
            "content": content,
            "tool_call_id": tool_call_id,
            "ts": time.time(),
        }
    # Agent messages that carry tool_calls render as a planning step.
    if tool_calls:
        calls = [
            {
                "name": c.get("name", "") if isinstance(c, dict) else str(c),
                "args": _jsonable(c.get("args", {})) if isinstance(c, dict) else {},
            }
            for c in tool_calls
        ]
        return {
            "kind": "agent",
            "role": "agent",
            "content": content,
            "tool_calls": calls,
            "ts": time.time(),
        }
    return {
        "kind": role,
        "role": role,
        "content": content,
        "ts": time.time(),
    }


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except Exception:
        return str(value)


def _coerce(value: Any) -> Any:
    """Make an RPC result JSON-serializable for the response body."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return _jsonable(value)
    if isinstance(value, (list, tuple)):
        return [_coerce(v) for v in value]
    # objects with an agent_encode / to_dict representation
    for attr in ("agent_encode", "to_dict", "dict"):
        func = getattr(value, attr, None)
        if callable(func):
            try:
                return _coerce(func())
            except Exception:
                pass
    try:
        return str(value)
    except Exception:
        return repr(value)


def _format_mcp_error(error: Any) -> str:
    if isinstance(error, dict):
        return str(error.get("message") or json.dumps(error))
    return str(error)


def _sse(event_type: str, data: dict[str, Any]) -> str:
    payload = json.dumps(_jsonable(data), ensure_ascii=False)
    return f"event: {event_type}\ndata: {payload}\n\n"