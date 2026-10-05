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
import json
import math
from pathlib import Path
import tempfile
import threading
import time
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode
import uuid

from dimos_lcm.std_msgs import String
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
import httpx
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
import rerun.blueprint as rrb
from starlette.responses import StreamingResponse
import uvicorn

from dimos.core.core import rpc
from dimos.core.global_config import global_config
from dimos.core.module import Module, ModuleConfig
from dimos.core.transport_factory import make_transport
from dimos.msgs.geometry_msgs.Twist import Twist
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.sensor_msgs.Image import Image
from dimos.utils.logging_config import setup_logger
from dimos.visualization.rerun.constants import RERUN_GRPC_PORT, RERUN_WEB_VIEWER_PORT
from dimos.web.console.frontend import INDEX_HTML
from dimos.web.console.settings import register_runtime_routes

if TYPE_CHECKING:
    from dimos.core.transport import PubSubTransport
    from dimos.web.console.settings import ConsoleRuntime

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
    "query_starting_location": Operation(
        "query_starting_location",
        "Starting location",
        "mcp",
        "Memory",
        description="Query the first aligned world position of this run; no movement.",
    ),
    "return_to_starting_location": Operation(
        "return_to_starting_location",
        "Return to start",
        "mcp",
        "Navigation",
        human_only=True,
        description="Navigate to this run's starting location after alignment.",
    ),
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
                "object_name": {"type": "string", "description": "Visible object name"},
            },
            "required": ["object_name"],
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
                "location_name": {"type": "string", "description": "Room or return-point name"},
            },
            "required": ["location_name"],
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
            },
        },
    ),
    # ---- Navigation (agent skills) ----
    "navigate_near_memory_tag": Operation(
        "navigate_near_memory_tag",
        "Navigate nearby",
        "mcp",
        "Navigation",
        primary=True,
        human_only=True,
        description="Approach a saved tag using the nearby-distance slider (default 1 m).",
        schema={
            "type": "object",
            "properties": {
                "location_id": {
                    "type": "string",
                    "description": "Exact saved ID from Query memory",
                },
            },
            "required": ["location_id"],
        },
    ),
    "navigate_to_memory_tag": Operation(
        "navigate_to_memory_tag",
        "Navigate precisely",
        "mcp",
        "Navigation",
        primary=False,
        human_only=True,
        description="Navigate to a saved location tag.",
        schema={
            "type": "object",
            "properties": {
                "location_id": {
                    "type": "string",
                    "description": "Exact saved ID from Query memory",
                },
            },
            "required": ["location_id"],
        },
    ),
    "stop_navigation": Operation(
        "stop_navigation",
        "Stop navigation",
        "mcp",
        "Navigation",
        description="Stop the current navigation run.",
    ),
    "navigation_state": Operation(
        "navigation_state",
        "Navigation state",
        "rpc",
        "Navigation",
        method="get_state",
        module="PersistentGo2Planner",
        description="Check planner state; starting a goal is not arrival.",
    ),
}


# ---------------------------------------------------------------------------
# Module
# ---------------------------------------------------------------------------
class RobotConsoleModuleConfig(ModuleConfig):
    port: int = 8090
    mcp_port: int = global_config.mcp_port
    rerun_web_port: int = RERUN_WEB_VIEWER_PORT
    rerun_grpc_port: int = RERUN_GRPC_PORT
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
        self._camera_transport: PubSubTransport[Any] | None = None
        self._puppy_transport: PubSubTransport[Any] | None = None
        self._navigation_transport: PubSubTransport[Any] | None = None
        self._arrival_sequence = 0
        self._camera_jpeg: bytes | None = None
        self._camera_timestamp = 0.0
        self._teleop_connected = False
        self._status: dict[str, Any] = {"agent_idle": None}
        self._clients: set[asyncio.Queue[Any]] = set()
        self.runtime: ConsoleRuntime | None = None
        self._csrf_token = uuid.uuid4().hex
        self._status_lock = asyncio.Lock()
        self._speaker_enabled = False
        self._speaker_generation = 0
        self._speech_queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue(maxsize=4)
        self._speech_task: asyncio.Task[None] | None = None
        self._last_reply_id: str | None = None

    # -- lifecycle --------------------------------------------------------
    @property
    def mcp_url(self) -> str:
        return f"http://127.0.0.1:{self.config.mcp_port}/mcp"

    @property
    def rerun_url(self) -> str:
        source = f"rerun+http://127.0.0.1:{self.config.rerun_grpc_port}/proxy"
        sources = [("url", source)]
        if self.runtime is not None:
            sources.append(("url", f"http://127.0.0.1:{self.config.port}/api/view/3d.rbl"))
        return f"http://127.0.0.1:{self.config.rerun_web_port}/?{urlencode(sources)}"

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
            self._agent_transport = make_transport("/agent")
            self._agent_transport.start()
            self._agent_transport.subscribe(self._on_agent)

            self._idle_transport = make_transport("/agent_idle")
            self._idle_transport.start()
            self._idle_transport.subscribe(self._on_idle)

            self._tool_transport = make_transport("/tool_streams")
            self._tool_transport.start()
            self._tool_transport.subscribe(self._on_tool)
            self._camera_transport = make_transport("/color_image", Image)
            self._camera_transport.start()
            self._camera_transport.subscribe(self._on_camera)
            self._puppy_transport = make_transport("/puppy_events")
            self._puppy_transport.start()
            self._puppy_transport.subscribe(self._on_puppy)
            self._navigation_transport = make_transport("/navigation_state", String)
            self._navigation_transport.start()
            self._navigation_transport.subscribe(self._on_navigation_state)
        except Exception:
            logger.exception("console: failed to set up agent streams")
            self._teardown_agent_streams()
            raise

    def _teardown_agent_streams(self) -> None:
        self._speaker_enabled = False
        if self._speech_task is not None and self._console_loop is not None:
            self._console_loop.call_soon_threadsafe(self._speech_task.cancel)
        for transport in (
            self._agent_transport,
            self._idle_transport,
            self._tool_transport,
            self._camera_transport,
            self._puppy_transport,
            self._navigation_transport,
        ):
            if transport is None:
                continue
            try:
                transport.stop()
            except Exception:
                logger.exception("console: transport stop failed")

    def _on_camera(self, image: Image) -> None:
        now = time.monotonic()
        if now - self._camera_timestamp < 0.1:
            return
        try:
            self._camera_jpeg = image.to_jpeg_bytes()
            self._camera_timestamp = now
        except Exception:
            logger.exception("console: camera encoding failed")

    def _on_puppy(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        if event.get("role") in ("user", "agent"):
            self._emit(
                {
                    "type": "message",
                    "role": event["role"],
                    "content": str(event.get("content", "")),
                    "tool_calls": [],
                    "source": str(event.get("source", "Puppy")),
                }
            )
        else:
            self._emit({"type": "tool", "name": "Puppy", "text": str(event.get("content", ""))})

    def _on_navigation_state(self, state: String) -> None:
        self._emit({"type": "tool", "name": "Navigation", "text": state.data})
        if state.data not in (
            "Arrived within 1 m of target",
            "Arrived within nearby threshold",
            "Arrived at target",
            "Arrived: tag image matched",
        ):
            return
        text = "Woof! We've arrived!"
        self._emit({"type": "message", "role": "agent", "content": text, "tool_calls": []})
        if self._console_loop is not None and self._speaker_enabled:
            self._arrival_sequence += 1
            self._console_loop.call_soon_threadsafe(
                self._queue_reply, text, f"navigation-arrival-{self._arrival_sequence}"
            )

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
            except asyncio.QueueFull:
                logger.warning("console: slow SSE client; discarding oldest event")
                client_queue.get_nowait()
                client_queue.put_nowait(event)

    def _on_agent(self, message: Any) -> None:
        payload = serialize_message(message)
        if payload is None:
            return
        self._status["agent_idle"] = False
        self._emit({"type": "message", **payload})
        self._emit({"type": "status", "agent_idle": False, **self._status})
        if payload["role"] == "user":
            self._last_reply_id = None
        elif payload["role"] == "agent" and not payload.get("tool_calls") and payload["content"]:
            reply_id = str(
                (message.get("id") if isinstance(message, dict) else getattr(message, "id", None))
                or payload["content"]
            )
            if self._console_loop is not None:
                self._console_loop.call_soon_threadsafe(
                    self._queue_reply, payload["content"], reply_id
                )

    def _queue_reply(self, text: str, reply_id: str) -> None:
        if not self._speaker_enabled or reply_id == self._last_reply_id:
            return
        if self.runtime is not None and not self.runtime.ready:
            return
        try:
            self._speech_queue.put_nowait((text, self._speaker_generation))
        except asyncio.QueueFull:
            self._emit(
                {
                    "type": "tool",
                    "name": "Go2 speaker",
                    "text": "Speech queue full; reply not spoken.",
                }
            )
            return
        self._last_reply_id = reply_id

    async def _speak_replies(self) -> None:
        while True:
            text, generation = await self._speech_queue.get()
            try:
                if self._speaker_enabled and generation == self._speaker_generation:
                    result = await self._call_rpc(
                        Operation("speak_agent_reply", "", "rpc", "", module="GO2Connection"),
                        {"text": text, "generation": generation},
                    )
                    self._emit({"type": "tool", "name": "Go2 speaker", "text": str(result)})
            finally:
                self._speech_queue.task_done()

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
        app = FastAPI(title="SEDAN GROUP Robot Console")

        @app.get("/api/camera.jpg")
        def camera() -> Response:
            if self._camera_jpeg is None or (self.runtime is not None and not self.runtime.ready):
                return Response("Camera not ready", status_code=503)
            if time.monotonic() - self._camera_timestamp > 3:
                return Response("Camera stream is stale", status_code=503)
            return Response(self._camera_jpeg, media_type="image/jpeg")

        @app.get("/api/view/3d.rbl")
        def world_blueprint() -> Response:
            blueprint = rrb.Blueprint(
                rrb.Spatial3DView(origin="world", name="3D", contents=["world/**"]),
                rrb.BlueprintPanel(state="hidden"),
                rrb.SelectionPanel(state="hidden"),
                rrb.TimePanel(state="hidden"),
                collapse_panels=True,
            )
            with tempfile.TemporaryDirectory(prefix="dimos-console-view-") as directory:
                path = Path(directory) / "3d.rbl"
                blueprint.save("dimos", path)
                return Response(
                    path.read_bytes(),
                    media_type="application/octet-stream",
                    headers={
                        "Access-Control-Allow-Origin": f"http://127.0.0.1:{self.config.rerun_web_port}"
                    },
                )

        @app.websocket("/api/teleop")
        async def teleop(socket: WebSocket) -> None:
            if (
                socket.query_params.get("token") != self._csrf_token
                or socket.headers.get("origin") != f"http://{socket.headers.get('host')}"
                or self.runtime is None
                or not self.runtime.ready
                or self._teleop_connected
            ):
                await socket.close(code=1008)
                return
            transport = make_transport("/tele_cmd_vel", Twist)
            self._teleop_connected = True
            started = False
            try:
                await socket.accept()
                prepared = await self._call_rpc(
                    Operation("switch_joystick", "", "rpc", "", module="GO2Connection"),
                    {"enable": True},
                )
                if not prepared.get("ok") or prepared.get("result") is not True:
                    await socket.send_json(
                        {"error": prepared.get("error", "Go2 refused joystick control.")}
                    )
                    await socket.close(code=1011)
                    return
                await asyncio.to_thread(transport.start)
                started = True
                await socket.send_json({"ready": True})
                while self.runtime.ready:
                    try:
                        payload = await asyncio.wait_for(socket.receive_json(), timeout=0.5)
                    except asyncio.TimeoutError:
                        await asyncio.to_thread(transport.publish, Twist.zero())
                        await socket.close(code=1008, reason="Keyboard command timeout")
                        break
                    keys = payload.get("keys") if isinstance(payload, dict) else None
                    if (
                        not isinstance(keys, list)
                        or len(keys) > 7
                        or any(
                            not isinstance(key, str)
                            or key not in {"w", "a", "s", "d", "q", "e", " "}
                            for key in keys
                        )
                    ):
                        await socket.close(code=1008, reason="Invalid keyboard command")
                        break
                    enabled = " " in keys
                    twist = Twist(
                        linear=Vector3(
                            0.3 * (("w" in keys) - ("s" in keys)) if enabled else 0,
                            0.3 * (("a" in keys) - ("d" in keys)) if enabled else 0,
                            0,
                        ),
                        angular=Vector3(
                            0, 0, 0.4 * (("q" in keys) - ("e" in keys)) if enabled else 0
                        ),
                    )
                    await asyncio.to_thread(transport.publish, twist)
            except WebSocketDisconnect:
                pass
            except Exception:
                logger.exception("console: keyboard control failed")
                raise
            finally:

                def cleanup() -> None:
                    try:
                        if started:
                            transport.publish(Twist.zero())
                    finally:
                        try:
                            transport.stop()
                        finally:
                            self._teleop_connected = False

                await asyncio.shield(asyncio.to_thread(cleanup))

        @app.middleware("http")
        async def protect_mutations(request: Request, call_next: Any) -> Response:
            if request.method == "POST":
                if request.headers.get("sec-fetch-site") == "cross-site":
                    return Response("Cross-site requests are not allowed", status_code=403)
                if (
                    self.runtime is not None
                    and request.headers.get("x-console-token") != self._csrf_token
                ):
                    return Response("Missing console token", status_code=403)
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            return cast("Response", response)

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
                "standalone": self.runtime is not None,
                "csrf_token": self._csrf_token,
            }

        @app.api_route("/api/navigation-distance", methods=["GET", "POST"])
        async def api_navigation_distance(request: Request) -> dict[str, Any]:
            if self.runtime is not None and not self.runtime.ready:
                return {"ok": False, "error": "Robot stack is not ready"}
            args = {}
            method = "nearby_navigation_status"
            if request.method == "POST":
                payload = await request.json()
                distance = payload.get("distance_m") if isinstance(payload, dict) else None
                if (
                    not isinstance(distance, (int, float))
                    or isinstance(distance, bool)
                    or not math.isfinite(distance)
                    or not 0.3 <= distance <= 3.0
                ):
                    return {"ok": False, "error": "Distance must be between 0.3 and 3.0 meters"}
                args = {"distance_m": float(distance)}
                method = "set_nearby_arrival_distance"
            return await self._call_rpc(
                Operation(method, "", "rpc", "", module="PersistentGo2Planner"), args
            )

        @app.api_route("/api/visual-arrival", methods=["GET", "POST"])
        async def api_visual_arrival(request: Request) -> dict[str, Any]:
            if self.runtime is not None and not self.runtime.ready:
                return {"ok": False, "error": "Robot stack is not ready"}
            method, args = "visual_arrival_status", {}
            if request.method == "POST":
                payload = await request.json()
                if not isinstance(payload, dict) or type(payload.get("enabled")) is not bool:
                    return {"ok": False, "error": "enabled must be a boolean"}
                if payload["enabled"] and payload.get("confirmed") is not True:
                    return {"ok": False, "error": "Confirm slow rotation and visual search first"}
                method, args = "configure_visual_arrival", {"enabled": payload["enabled"]}
            return await self._call_rpc(
                Operation(method, "", "rpc", "", module="PersistentGo2Planner"), args
            )

        @app.api_route("/api/murmur", methods=["GET", "POST"])
        async def api_murmur(request: Request) -> dict[str, Any]:
            if self.runtime is None or not self.runtime.ready:
                return {"ok": False, "error": "Robot stack is not ready"}
            method, args = "reply_speaker_status", {}
            if request.method == "POST":
                payload = await request.json()
                if not isinstance(payload, dict) or type(payload.get("enabled")) is not bool:
                    return {"ok": False, "error": "enabled must be a boolean"}
                method, args = "configure_puppy_murmur", {"enabled": payload["enabled"]}
            return await self._call_rpc(
                Operation(method, "", "rpc", "", module="GO2Connection"), args
            )

        @app.api_route("/api/tagging", methods=["GET", "POST"])
        async def api_tagging(request: Request) -> dict[str, Any]:
            if self.runtime is not None and not self.runtime.ready:
                return {"ok": False, "error": "Robot stack is not ready"}
            args = {}
            method = "automatic_tagging_status"
            if request.method == "POST":
                payload = await request.json()
                if not isinstance(payload, dict) or type(payload.get("enabled")) is not bool:
                    return {"ok": False, "error": "enabled must be a boolean"}
                args = {"enabled": payload["enabled"]}
                method = "set_automatic_tagging"
            return await self._call_rpc(
                Operation(method, "", "rpc", "", module="SpatialMemory"), args
            )

        @app.api_route("/api/speaker", methods=["GET", "POST"])
        async def api_speaker(request: Request) -> dict[str, Any]:
            if self.runtime is None or not self.runtime.ready:
                return {"ok": False, "error": "Robot stack is not ready"}
            if request.method == "GET":
                return await self._call_rpc(
                    Operation("reply_speaker_status", "", "rpc", "", module="GO2Connection"), {}
                )
            payload = await request.json()
            if not isinstance(payload, dict) or type(payload.get("enabled")) is not bool:
                return {"ok": False, "error": "enabled must be a boolean"}
            enabled = payload["enabled"]
            if enabled and payload.get("confirmed") is not True:
                return {"ok": False, "error": "Confirm maximum-volume Go2 speaker playback first."}
            if not enabled:
                self._speaker_enabled = False
            result = await self._call_rpc(
                Operation("configure_reply_speaker", "", "rpc", "", module="GO2Connection"),
                {"enabled": enabled},
            )
            if result.get("ok") and isinstance(result.get("result"), dict):
                self._speaker_enabled = result["result"]["enabled"]
                self._speaker_generation = result["result"]["generation"]
                self._last_reply_id = None
                if self._speaker_enabled and (
                    self._speech_task is None or self._speech_task.done()
                ):
                    self._speech_task = asyncio.create_task(self._speak_replies())
            return result

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
            if not isinstance(key, str) or key not in OPERATIONS:
                return {"ok": False, "error": f"unknown operation: {key!r}"}
            if self.runtime is not None and not self.runtime.ready:
                return {"ok": False, "error": "Robot stack is not ready"}
            if OPERATIONS[key].human_only and payload.get("confirmed") is not True:
                return {"ok": False, "error": "Human confirmation is required"}
            return await self._dispatch(key, args)

        @app.post("/api/chat")
        async def api_chat(payload: dict[str, Any]) -> dict[str, Any]:
            value = payload.get("message") or payload.get("text") or ""
            if not isinstance(value, str):
                return {"ok": False, "error": "message must be text"}
            text = value.strip()
            if not text:
                return {"ok": False, "error": "empty message"}
            if self.runtime is not None and not self.runtime.ready:
                return {"ok": False, "error": "Robot stack is not ready"}
            try:
                await asyncio.to_thread(self._publish_human_input, text)
            except Exception:
                logger.exception("console: chat delivery failed")
                return {"ok": False, "error": "Chat transport failed; message was not sent"}
            return {"ok": True, "echo": text}

        @app.get("/api/refresh-status")
        async def api_refresh_status() -> dict[str, Any]:
            return await self._refresh_status()

        if self.runtime is not None:
            register_runtime_routes(app, self.runtime)

        @app.get("/api/diagnostics")
        async def api_diagnostics() -> dict[str, Any]:
            if self.runtime is not None and not self.runtime.ready:
                return {"ok": False, "error": "Robot stack is not ready"}
            results = await asyncio.gather(
                self._call_mcp(Operation("server_status", "", "mcp", ""), {}),
                self._call_mcp(Operation("list_modules", "", "mcp", ""), {}),
                self._mcp_request("tools/list", {}),
            )
            return {"server": results[0], "modules": results[1], "tools": results[2]}

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

            return StreamingResponse(generator(), media_type="text/event-stream")

        return app

    # -- dispatch ---------------------------------------------------------
    async def _dispatch(self, key: str, args: dict[str, Any]) -> dict[str, Any]:
        op = OPERATIONS[key]
        if not isinstance(args, dict):
            return {"ok": False, "error": "arguments must be an object"}
        properties = op.schema.get("properties", {})
        if set(args) - set(properties):
            return {"ok": False, "error": "Unknown arguments"}
        for name in op.schema.get("required", []):
            if not isinstance(args.get(name), str) or not args[name].strip():
                return {"ok": False, "error": f"{name} is required"}
        if any(not isinstance(value, str) for value in args.values()):
            return {"ok": False, "error": "Arguments must be text"}
        if op.kind == "rpc":
            return await self._call_rpc(op, args)
        if op.kind == "mcp":
            return await self._call_mcp(op, args)
        return {"ok": False, "error": f"unknown kind for {key}"}

    async def _call_rpc(self, op: Operation, args: dict[str, Any]) -> dict[str, Any]:
        module = self.config.map_module if op.module == DEFAULT_MAP_MODULE else op.module
        address = f"{module}/{op.method}"
        return await self._run_blocking(_rpc_call, self.rpc, address, args)

    async def _call_mcp(self, op: Operation, args: dict[str, Any]) -> dict[str, Any]:
        data = await self._mcp_request(
            "tools/call",
            {"name": op.key, "arguments": args, "_meta": {"progressToken": uuid.uuid4().hex}},
        )
        if data.get("ok") is False:
            return data
        result = data.get("result")
        if not isinstance(result, dict) or "content" not in result:
            return {"ok": False, "error": "MCP response has no tool content"}
        content = result["content"]
        if isinstance(content, list):
            content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
        if result.get("isError") or str(content).startswith(
            ("Error running tool", "Tool not found:", "Cannot start '")
        ):
            return {"ok": False, "error": str(content)}
        return {"ok": True, "result": _coerce(content)}

    async def _mcp_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params,
        }
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(self.mcp_url, json=request)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError):
            logger.exception("console: MCP request failed")
            return {"ok": False, "error": "MCP request failed; check stack status"}

        if not isinstance(data, dict):
            return {"ok": False, "error": "Invalid MCP response"}
        if "error" in data:
            return {"ok": False, "error": _format_mcp_error(data["error"])}
        return data

    async def _run_blocking(self, func: Any, *args: Any) -> dict[str, Any]:
        """Run a blocking RPC call on a dedicated pool (keeps the loop free)."""
        result = await asyncio.to_thread(func, *args)
        return cast("dict[str, Any]", result)

    # -- chat / status ----------------------------------------------------
    def _publish_human_input(self, text: str) -> None:
        transport = make_transport("/human_input")
        try:
            transport.start()
            transport.publish(text)
        finally:
            transport.stop()

    async def _refresh_status(self) -> dict[str, Any]:
        async with self._status_lock:
            if self.runtime is not None:
                self._status.update(stack=self.runtime.status())
                if not self.runtime.ready:
                    self._status.update(
                        agent_idle=None, navigation_ready=None, alignment_status=None
                    )
                    return dict(self._status)
            status = await self._run_blocking(
                _status_probe, self.rpc, self.config.map_module, self._status
            )
            status.pop("agent_idle", None)
            self._status.update(status)
            self._emit({"type": "status", **self._status})
            return dict(self._status)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _rpc_call(rpc: Any, address: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        result, unsub = rpc.call_sync(address, ([], args), rpc_timeout=120.0)
        try:
            unsub()
        except Exception:
            pass
        return {"ok": True, "result": _coerce(result)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _status_probe(rpc: Any, map_module: str, base: dict[str, Any]) -> dict[str, Any]:
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
                "id": c.get("id") if isinstance(c, dict) else None,
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
