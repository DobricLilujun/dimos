# Copyright 2025-2026 Dimensional Inc.
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
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
import concurrent.futures
from dataclasses import replace
import json
import os
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any
import uuid

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
import uvicorn

from dimos.agents.annotation import skill
from dimos.agents.capabilities import CapabilityRegistry
from dimos.agents.mcp import tool_stream
from dimos.agents.mcp.task_sequence import TaskSequenceExecutor
from dimos.agents.skill_result import SkillResult
from dimos.core.core import rpc
from dimos.core.module import Module
from dimos.core.rpc_client import RpcCall, RPCClient
from dimos.core.transport_factory import make_transport
from dimos.utils.logging_config import setup_logger

if TYPE_CHECKING:
    from dimos.core.module import SkillInfo

logger = setup_logger()


_SSE_KEEPALIVE_INTERVAL = 20.0  # seconds

# How long a `tools/call` waits for a capability held by a short, self-completing
# (instant) skill before refusing. Well under the MCP client's 120s HTTP timeout.
# Background holders run until stopped, so they are never waited on (see
# `_can_wait` in `_handle_tools_call`).
DEFAULT_CAP_ACQUIRE_TIMEOUT = 30.0  # seconds

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)
app.state.skills = []
app.state.skills_by_name = {}
app.state.rpc_calls = {}
app.state.sse_queues = []
app.state.event_loop = None
app.state.cap_registry = CapabilityRegistry()
app.state.cap_acquire_timeout = DEFAULT_CAP_ACQUIRE_TIMEOUT
app.state.task_sequence = None
app.state.sequence_navigation_status = None
app.state.sequence_navigation_cancel = None


def _jsonrpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_result_text(req_id: Any, text: str, *, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return _jsonrpc_result(req_id, result)


def _jsonrpc_skill_result(req_id: Any, result: SkillResult) -> dict[str, Any]:
    return _jsonrpc_result(
        req_id,
        {
            "content": result.agent_encode(),
            "structuredContent": json.loads(result.agent_encode()[0]["text"]),
            **({"isError": True} if not result.success else {}),
        },
    )


def _jsonrpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _handle_initialize(req_id: Any) -> dict[str, Any]:
    return _jsonrpc_result(
        req_id,
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}, "logging": {}},
            "serverInfo": {"name": "dimensional", "version": "1.0.0"},
        },
    )


def _handle_tools_list(req_id: Any, skills: list[SkillInfo]) -> dict[str, Any]:
    tools = []

    for s in skills:
        schema = json.loads(s.args_schema)
        description = schema.pop("description", None)
        schema.pop("title", None)
        tool: dict[str, Any] = {"name": s.func_name, "inputSchema": schema}
        if description:
            tool["description"] = description
        if s.uses or s.lifecycle != "instant" or s.completion is not None:
            tool["_meta"] = {
                "dimos/uses": list(s.uses),
                "dimos/lifecycle": s.lifecycle,
                "dimos/completion": s.completion,
            }
        tools.append(tool)

    return _jsonrpc_result(req_id, {"tools": tools})


async def _handle_tools_call(
    req_id: Any,
    params: dict[str, Any],
    rpc_calls: dict[str, Any],
    *,
    sequence_operation_id: str | None = None,
) -> dict[str, Any]:
    name = params.get("name", "")
    args: dict[str, Any] = params.get("arguments") or {}
    meta = params.get("_meta") or {}
    progress_token = meta.get("progressToken")

    rpc_call = rpc_calls.get(name)
    if rpc_call is None:
        logger.warning("MCP tool not found", tool=name)
        if sequence_operation_id is not None:
            return _jsonrpc_skill_result(
                req_id, SkillResult.fail("INVALID_INPUT", f"Tool not found: {name}")
            )
        return _jsonrpc_result_text(req_id, f"Tool not found: {name}", is_error=True)

    skill_info = app.state.skills_by_name.get(name)
    if sequence_operation_id is not None and (
        skill_info is None
        or skill_info.completion not in ("result", "navigation")
        or skill_info.lifecycle != "instant"
    ):
        return _jsonrpc_skill_result(
            req_id,
            SkillResult.fail(
                "INVALID_INPUT", f"Tool has no supported bounded completion contract: {name}"
            ),
        )
    uses: list[str] = list(skill_info.uses) if skill_info is not None else []
    lifecycle = skill_info.lifecycle if skill_info is not None else "instant"
    cap_registry: CapabilityRegistry = app.state.cap_registry
    sequence: TaskSequenceExecutor | None = app.state.task_sequence
    if (
        sequence_operation_id is None
        and sequence is not None
        and ("movement" in uses or name.startswith(("stop_", "end_", "cancel_")))
        and name != "cancel_task_sequence"
    ):
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: sequence.pause(f"Preempted by tool '{name}'")
        )

    # A per-invocation token scopes the capability hold, so a stale invocation's
    # teardown can't release a hold that a newer same-tool invocation took over.
    acquire_token = uuid.uuid4().hex
    if uses:

        def _can_wait(holder: str) -> bool:
            # Wait only on instant holders; they release when they return.
            # Background holders run until explicitly stopped, so refuse instead
            # of blocking until the timeout.
            info = app.state.skills_by_name.get(holder)
            return (info.lifecycle if info is not None else "instant") != "background"

        # Run the (possibly blocking) acquire off the event loop so waiting for a
        # busy capability doesn't stall the server.
        conflict = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: cap_registry.acquire(
                uses,
                tool_name=name,
                token=acquire_token,
                timeout=app.state.cap_acquire_timeout,
                can_wait=_can_wait,
            ),
        )
        if conflict is not None:
            cap, holder = conflict
            logger.info(
                "MCP tool refused (capability busy)",
                tool=name,
                cap=cap,
                holder=holder,
                snapshot=cap_registry.snapshot(),
            )
            # A background holder has a stop tool to call; an instant holder is
            # waited on above, so reaching here means it outlasted the timeout.
            holder_info = app.state.skills_by_name.get(holder)
            holder_lifecycle = holder_info.lifecycle if holder_info is not None else "instant"
            if holder_lifecycle == "background":
                advice = "Call the appropriate stop tool first, then retry."
            else:
                advice = "It is taking longer than expected; wait a moment and then retry."
            message = f"Cannot start '{name}': capability '{cap}' is held by '{holder}'. {advice}"
            if sequence_operation_id is not None:
                return _jsonrpc_skill_result(req_id, SkillResult.fail("INVALID_STATE", message))
            return _jsonrpc_result_text(req_id, message, is_error=True)

    logger.info("MCP tool call", tool=name, args=args, progress_token=progress_token)
    t0 = time.monotonic()

    # _mcp_context is a reserved kwarg consumed by the `@skill` wrapper; it never
    # reaches the user-visible skill signature. The acquire token rides along so
    # a background skill's ToolStream can stamp it on its stop frame for release.
    call_kwargs = dict(args)
    mcp_context: dict[str, Any] = {}
    if progress_token is not None:
        mcp_context["progress_token"] = progress_token
    if uses:
        mcp_context["acquire_token"] = acquire_token
    if sequence_operation_id is not None:
        mcp_context["sequence_operation_id"] = sequence_operation_id
    if mcp_context:
        call_kwargs["_mcp_context"] = mcp_context

    # Track whether we still hold the caps so we can release on failure even
    # for background skills. On success the background skill keeps them until
    # its tool-stream closes.
    caps_held = bool(uses)
    try:
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: rpc_call(**call_kwargs)
            )
            if (
                sequence_operation_id is not None
                and skill_info is not None
                and skill_info.completion == "navigation"
            ):
                status_call = app.state.sequence_navigation_status
                if status_call is None:
                    raise RuntimeError("Navigation completion adapter unavailable")
                while True:
                    status = await asyncio.get_running_loop().run_in_executor(
                        None, lambda: status_call(operation_id=sequence_operation_id)
                    )
                    if status.get("operation_id") != sequence_operation_id:
                        raise RuntimeError("Navigation completion operation ID mismatch")
                    if status.get("status") != "running":
                        result = (
                            SkillResult.ok("Navigation arrival verified")
                            if status.get("status") == "completed"
                            else SkillResult.fail(
                                "EXECUTION_FAILED", f"Navigation {status.get('status', 'unknown')}"
                            )
                        )
                        break
                    await asyncio.sleep(0.1)
        except Exception as e:
            logger.exception("MCP tool error", tool=name, duration=f"{time.monotonic() - t0:.3f}s")
            return _jsonrpc_result_text(req_id, f"Error running tool '{name}': {e}", is_error=True)

        if lifecycle == "background":
            # Hand ownership of the caps off to the tool-stream lifecycle.
            caps_held = False
    finally:
        if caps_held:
            cap_registry.release_by_token(acquire_token)

    duration = f"{time.monotonic() - t0:.3f}s"
    response = str(result)[:200]

    if hasattr(result, "agent_encode"):
        logger.info("MCP tool done", tool=name, duration=duration, response=response)
        if isinstance(result, SkillResult):
            return _jsonrpc_skill_result(req_id, result)
        return _jsonrpc_result(req_id, {"content": result.agent_encode()})

    logger.info("MCP tool done", tool=name, duration=duration, response=response)
    return _jsonrpc_result_text(req_id, str(result))


async def handle_request(
    request: dict[str, Any],
    skills: list[SkillInfo],
    rpc_calls: dict[str, Any],
) -> dict[str, Any] | None:
    """Handle a single MCP JSON-RPC request.

    Returns None for JSON-RPC notifications (no ``id``), which must not
    receive a response.
    """
    method = request.get("method", "")
    params = request.get("params", {}) or {}
    req_id = request.get("id")

    # JSON-RPC notifications have no "id" -- the server must not reply.
    if "id" not in request:
        return None

    if method == "initialize":
        return _handle_initialize(req_id)
    if method == "tools/list":
        return _handle_tools_list(req_id, skills)
    if method == "tools/call":
        return await _handle_tools_call(req_id, params, rpc_calls)
    return _jsonrpc_error(req_id, -32601, f"Unknown: {method}")


@app.post("/mcp")
async def mcp_endpoint(request: Request) -> Response:
    raw = await request.body()
    try:
        body = json.loads(raw)
    except Exception:
        logger.exception("POST /mcp JSON parse failed")
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=400,
        )

    result = await handle_request(body, request.app.state.skills, request.app.state.rpc_calls)

    if result is None:
        return Response(status_code=204)
    return JSONResponse(result)


def _sse_frame(data: dict[str, Any]) -> str:
    """Format a JSON-RPC message as an SSE ``event: message`` frame."""
    return f"event: message\ndata: {json.dumps(data)}\n\n"


def _fan_out_to_sse_queues(msg: dict[str, Any]) -> None:
    """LCM subscriber callback: forward a tool-stream frame to every active SSE client.

    Also releases capabilities held by a background skill when its tool-stream
    closes (signaled by a ``dimos/tool_stopped`` frame).
    """
    if msg.get("method") == tool_stream.TOOL_STREAM_STOPPED_METHOD:
        params = msg.get("params") or {}
        token = params.get("token")
        if token:
            released = app.state.cap_registry.release_by_token(token)
            if released:
                logger.info(
                    "Capabilities released on tool-stream stop",
                    holder=params.get("tool_name"),
                    token=token,
                    released=released,
                )
    loop = app.state.event_loop
    if loop is None:
        return
    for queue in list(app.state.sse_queues):
        try:
            asyncio.run_coroutine_threadsafe(queue.put(msg), loop)
        except RuntimeError:
            pass


@app.get("/mcp")
async def mcp_sse_endpoint() -> StreamingResponse:
    """Persistent server-to-client SSE channel for MCP notifications.

    This is the Streamable-HTTP transport's out-of-band channel for
    server-initiated messages.  Every tool-stream update is fanned out here,
    so the subscription is live for the full client session and independent
    of any particular ``tools/call`` request.
    """
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    # Remember the loop so the LCM subscriber (running on an LCM thread)
    # can schedule queue.put via run_coroutine_threadsafe.
    app.state.event_loop = asyncio.get_running_loop()
    app.state.sse_queues.append(queue)

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            # Initial comment flushes the response headers and unblocks
            # any synchronous client that's waiting on iter_lines().
            yield ": connected\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=_SSE_KEEPALIVE_INTERVAL)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if msg is None:
                    return
                yield _sse_frame(msg)
        except asyncio.CancelledError:
            pass
        finally:
            try:
                app.state.sse_queues.remove(queue)
            except ValueError:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


class McpServer(Module):
    _uvicorn_server: uvicorn.Server | None = None
    _serve_future: concurrent.futures.Future[None] | None = None
    _tool_stream_cleanup: Callable[[], None] | None = None
    _sequence_progress_transport: Any = None

    @rpc
    def start(self) -> None:
        super().start()
        self._start_server()
        self._tool_stream_cleanup = tool_stream.subscribe(_fan_out_to_sse_queues)

    @rpc
    def stop(self) -> None:
        if app.state.task_sequence is not None:
            app.state.task_sequence.close()
            app.state.task_sequence = None
        if self._sequence_progress_transport is not None:
            self._sequence_progress_transport.stop()
            self._sequence_progress_transport = None
        if self._tool_stream_cleanup is not None:
            self._tool_stream_cleanup()
            self._tool_stream_cleanup = None

        for queue in list(app.state.sse_queues):
            try:
                queue.put_nowait(None)
            except Exception:
                pass
        app.state.sse_queues.clear()

        if self._uvicorn_server:
            self._uvicorn_server.should_exit = True
            loop = self._loop
            if loop is not None and self._serve_future is not None:
                self._serve_future.result(timeout=5.0)
            self._uvicorn_server = None
            self._serve_future = None
        super().stop()

    @rpc
    def on_system_modules(self, modules: list[RPCClient]) -> None:
        # TODO: this is a bit hacky, also not thread-safe
        assert self.rpc is not None
        app.state.skills = [
            skill_info for module in modules for skill_info in (module.get_skills() or [])
        ]
        app.state.skills_by_name = {s.func_name: s for s in app.state.skills}
        app.state.rpc_calls = {
            skill_info.func_name: RpcCall(
                None, self.rpc, skill_info.func_name, skill_info.class_name, []
            )
            for skill_info in app.state.skills
        }
        navigation = app.state.skills_by_name.get("navigate_near_memory_tag")
        if navigation is not None:
            available = RpcCall(
                None, self.rpc, "sequence_navigation_available", navigation.class_name, []
            )
            if not available():
                app.state.skills = [
                    replace(info, completion=None)
                    if info.func_name == navigation.func_name
                    else info
                    for info in app.state.skills
                ]
                app.state.skills_by_name = {s.func_name: s for s in app.state.skills}
            app.state.sequence_navigation_status = RpcCall(
                None, self.rpc, "sequence_navigation_status", navigation.class_name, []
            )
            app.state.sequence_navigation_cancel = RpcCall(
                None, self.rpc, "cancel_sequence_navigation", navigation.class_name, []
            )
        if app.state.task_sequence is None:
            self._sequence_progress_transport = make_transport(tool_stream.TOOL_STREAM_TOPIC)
            self._sequence_progress_transport.start()
            state_dir = Path(
                os.environ.get(
                    "DIMOS_TASK_SEQUENCE_STATE_DIR",
                    str(
                        Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
                        / "dimos"
                    ),
                )
            )

            def dispatch(name: str, arguments: dict[str, Any], operation_id: str) -> dict[str, Any]:
                response = asyncio.run(
                    _handle_tools_call(
                        operation_id,
                        {"name": name, "arguments": arguments},
                        app.state.rpc_calls,
                        sequence_operation_id=operation_id,
                    )
                )
                result: dict[str, Any] = response["result"]
                return result

            def cancel_operation(name: str, operation_id: str) -> bool:
                info = app.state.skills_by_name.get(name)
                if info is None or info.completion != "navigation":
                    return False
                call = app.state.sequence_navigation_cancel
                return bool(call is not None and call(operation_id=operation_id))

            last_notification: str | None = None

            def publish(status: dict[str, Any]) -> None:
                nonlocal last_notification
                summary = {
                    key: status.get(key)
                    for key in ("id", "status", "active_index", "reason", "quiescent")
                }
                summary["steps"] = [
                    {"id": step["id"], "tool": step["tool"], "status": step["status"]}
                    for step in status.get("steps", [])
                ]
                text = json.dumps(summary)
                if text == last_notification:
                    return
                last_notification = text
                try:
                    self._sequence_progress_transport.publish(
                        tool_stream.make_notification("task_sequence", text)
                    )
                except Exception:
                    logger.exception("Sequence progress notification failed")

            app.state.task_sequence = TaskSequenceExecutor(
                state_dir / "task_sequence.json",
                lambda: app.state.skills_by_name,
                dispatch,
                cancel_operation,
                publish,
            )

    def _sequence(self) -> TaskSequenceExecutor:
        executor: TaskSequenceExecutor | None = app.state.task_sequence
        if executor is None:
            raise RuntimeError("Task sequence executor is not ready")
        return executor

    @skill
    def start_task_sequence(self, steps: list[dict[str, Any]]) -> str:
        """Validate and start an ordered durable tool plan (at most 32 steps).

        Each step has tool, arguments, completion ('result' or 'navigation'),
        and optional timeout in seconds. Use only tools advertising
        dimos/completion. Started navigation is not arrival. Errors pause the
        plan; only a confirmed human action may resume or retry it.
        """
        return json.dumps(self._sequence().start(steps))

    @skill
    def task_sequence_status(self) -> str:
        """Get ordered sequence steps, actual results, active index and pause reason."""
        return json.dumps(self._sequence().status())

    @skill
    def pause_task_sequence(self) -> str:
        """Pause the current sequence and stop its active operation where supported."""
        return json.dumps(self._sequence().pause())

    @skill
    def cancel_task_sequence(self) -> str:
        """Cancel the current sequence; never run its remaining steps."""
        return json.dumps(self._sequence().cancel())

    @rpc
    def resume_task_sequence(self, sequence_id: str, retry_failed: bool = False) -> dict[str, Any]:
        """Resume only after Console human confirmation; not an MCP/agent tool."""
        return self._sequence().resume(sequence_id, retry_failed=retry_failed)

    @rpc
    def preempt_task_sequence(self, reason: str = "Manual takeover") -> dict[str, Any]:
        """Console must call before Stop, keyboard takeover, or manual movement."""
        return self._sequence().pause(reason)

    @rpc
    def get_task_sequence_status(self) -> dict[str, Any]:
        return self._sequence().status()

    @skill
    def server_status(self) -> str:
        """Get MCP server status: main process PID, deployed modules, and skill count."""
        from dimos.core.run_registry import get_most_recent

        skills: list[SkillInfo] = app.state.skills
        modules = list(dict.fromkeys(s.class_name for s in skills))
        entry = get_most_recent()
        pid = entry.pid if entry else os.getpid()
        return json.dumps(
            {
                "pid": pid,
                "modules": modules,
                "skills": [s.func_name for s in skills],
            }
        )

    @skill
    def list_modules(self) -> str:
        """List deployed modules and their skills."""
        skills: list[SkillInfo] = app.state.skills
        modules: dict[str, list[str]] = {}
        for s in skills:
            modules.setdefault(s.class_name, []).append(s.func_name)
        return json.dumps({"modules": modules})

    @skill
    def agent_send(self, message: str) -> str:
        """Send a message to the running DimOS agent over the active transport."""
        if not message:
            raise ValueError("Message cannot be empty")

        transport = make_transport("/human_input")
        try:
            transport.start()
            transport.publish(message)
            return f"Message sent to agent: {message[:100]}"
        finally:
            transport.stop()

    def _start_server(self, port: int | None = None) -> None:
        from dimos.core.global_config import global_config

        _port = port if port is not None else global_config.mcp_port
        _host = global_config.listen_host
        config = uvicorn.Config(app, host=_host, port=_port, log_level="warning", access_log=False)
        server = uvicorn.Server(config)
        self._uvicorn_server = server
        loop = self._loop
        assert loop is not None
        self._serve_future = asyncio.run_coroutine_threadsafe(server.serve(), loop)
