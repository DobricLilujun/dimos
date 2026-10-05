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

"""Tests for the SEDAN GROUP robot console.

Exercises the console module's web app (FastAPI) and its live event stream,
using the ASGI transport directly (no worker / robot / network / LLM). The RPC
/ MCP / human-input side-effects are monkey-patched to fakes so the tests are
deterministic and offline.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
import inspect
import threading
import time
from typing import Any

from dimos_lcm.std_msgs import String
from fastapi.testclient import TestClient
import httpx
from langchain_core.messages import AIMessage, ToolMessage
import pytest
from starlette.websockets import WebSocketDisconnect
import uvicorn

from dimos.agents.skills.navigation import NavigationSkillContainer
from dimos.web.console.frontend import INDEX_HTML
from dimos.web.console.module import (
    OPERATIONS,
    RobotConsoleModule,
    _content_to_text,
    _normalize_message,
    serialize_message,
)
from dimos.web.console.settings import ConsoleRuntime

PORT = 8191


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def module(monkeypatch: pytest.MonkeyPatch) -> Iterator[RobotConsoleModule]:
    m = RobotConsoleModule(port=PORT, mcp_port=9990, rerun_web_port=9878)
    # Stop the module from opening a real RPC / transport / web server.
    monkeypatch.setattr(m, "_setup_agent_streams", lambda: None)
    monkeypatch.setattr(m, "_teardown_agent_streams", lambda: None)
    try:
        yield m
    finally:
        # Reaping the module closes its event-loop thread (``_loop_thread``);
        # without it the run_forever thread would leak and trip the conftest
        # thread-leak monitor. The module was never started, so this only
        # tears down the loop + rpc session (idempotent, safe to call twice).
        m._close_module()


@pytest.fixture()
async def client(module: RobotConsoleModule) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=module._build_app())
    async with httpx.AsyncClient(transport=transport, base_url=f"http://localhost:{PORT}") as c:
        yield c


# ---------------------------------------------------------------------------
# front-end (static)
# ---------------------------------------------------------------------------
def test_frontend_has_required_sections() -> None:
    html = INDEX_HTML
    assert "SEDAN GROUP" in html, "branding missing"
    assert "Robot Console" in html
    assert 'id="rr"' in html and "iframe" in html, "Rerun embed missing"
    assert "/events" in html, "SSE endpoint missing"
    assert "/api/config" in html and "/api/action" in html and "/api/chat" in html
    assert "Agent chat" in html
    assert "control deck" in html.lower()
    # tool input / output surface
    assert "input" in html and "output" in html


# ---------------------------------------------------------------------------
# index + config
# ---------------------------------------------------------------------------
async def test_index_and_config(client: httpx.AsyncClient) -> None:
    idx = await client.get("/")
    assert idx.status_code == 200
    assert "SEDAN GROUP" in idx.text

    cfg = await client.get("/api/config")
    assert cfg.status_code == 200
    data = cfg.json()
    assert (
        data["rerun_url"]
        == "http://127.0.0.1:9878/?url=rerun%2Bhttp%3A%2F%2F127.0.0.1%3A9877%2Fproxy"
    )
    assert "operations" in data
    keys = {op["key"] for op in data["operations"]}
    for expected in (
        "confirm_alignment",
        "reject_alignment",
        "save_map",
        "finish_startup_capture",
        "cancel_startup_rotation",
        "alignment_status",
        "navigation_ready",
        "tag_object",
        "tag_location",
        "query_memory_tags",
        "navigate_to_memory_tag",
        "stop_navigation",
    ):
        assert expected in keys, f"missing operation {expected}"
    kinds = {op["kind"] for op in data["operations"]}
    assert kinds == {"rpc", "mcp"}
    confirm = next(op for op in data["operations"] if op["key"] == "confirm_alignment")
    assert confirm["human_only"] is True
    assert confirm["kind"] == "rpc"


async def test_camera_reports_unavailable_and_returns_fresh_jpeg(client, module, mocker):
    assert (await client.get("/api/camera.jpg")).status_code == 503
    image = mocker.Mock()
    image.to_jpeg_bytes.return_value = b"test-jpeg"
    module._on_camera(image)
    response = await client.get("/api/camera.jpg")
    assert response.content == b"test-jpeg"
    assert response.headers["content-type"] == "image/jpeg"
    module._camera_timestamp = time.monotonic() - 4
    assert (await client.get("/api/camera.jpg")).text == "Camera stream is stale"


async def test_console_3d_blueprint_is_available_without_changing_original_viewer(client):
    response = await client.get("/api/view/3d.rbl")
    assert response.status_code == 200
    assert response.content[:4] == b"RRF2"
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:9878"


@pytest.fixture
def keyboard_client(module, tmp_path, mocker):
    module.runtime = ConsoleRuntime(tmp_path, tmp_path / "settings.json")
    module.runtime._state = "running"
    transport = mocker.patch("dimos.web.console.module.make_transport").return_value
    mocker.patch.object(module, "_call_rpc", return_value={"ok": True, "result": True})
    with TestClient(module._build_app()) as client:
        yield client, module, transport


def test_keyboard_requires_valid_token_and_same_origin(keyboard_client):
    client, module, transport = keyboard_client
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/teleop?token=wrong", headers={"Origin": "http://testserver"}
        ):
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/api/teleop?token={module._csrf_token}", headers={"Origin": "http://evil.test"}
        ):
            pass
    transport.publish.assert_not_called()


def test_keyboard_deadman_and_disconnect_send_stop(keyboard_client):
    client, module, transport = keyboard_client
    commands = []
    event = threading.Event()
    stopped = threading.Event()
    transport.stop.side_effect = stopped.set

    def publish(twist):
        commands.append(twist)
        event.set()

    transport.publish.side_effect = publish
    with client.websocket_connect(
        f"/api/teleop?token={module._csrf_token}", headers={"Origin": "http://testserver"}
    ) as socket:
        socket.send_json({"keys": ["w"]})
        assert event.wait(2)
        assert commands[-1].linear.x == 0
        event.clear()
        socket.send_json({"keys": [" ", "w", "q"]})
        assert event.wait(2)
        assert commands[-1].linear.x == 0.3
        assert commands[-1].angular.z == 0.4
    assert stopped.wait(2)
    assert commands[-1].linear.x == 0
    assert commands[-1].angular.z == 0
    transport.stop.assert_called_once()


def test_keyboard_watchdog_stops_when_browser_commands_cease(keyboard_client):
    client, module, transport = keyboard_client
    with client.websocket_connect(
        f"/api/teleop?token={module._csrf_token}", headers={"Origin": "http://testserver"}
    ) as socket:
        assert socket.receive_json() == {"ready": True}
        socket.send_json({"keys": [" ", "w"]})
        with pytest.raises(WebSocketDisconnect):
            socket.receive_text()
    assert transport.publish.call_args_list[0].args[0].linear.x == 0.3
    assert transport.publish.call_args_list[-1].args[0].linear.x == 0


def test_invalid_keyboard_command_closes_with_stop(keyboard_client):
    client, module, transport = keyboard_client
    with client.websocket_connect(
        f"/api/teleop?token={module._csrf_token}", headers={"Origin": "http://testserver"}
    ) as socket:
        assert socket.receive_json() == {"ready": True}
        socket.send_json({"keys": ["unknown"]})
        with pytest.raises(WebSocketDisconnect):
            socket.receive_text()
    assert transport.publish.call_args_list[-1].args[0].linear.x == 0


def test_keyboard_prepares_go2_joystick_and_refuses_failed_preparation(keyboard_client, mocker):
    client, module, transport = keyboard_client
    mocker.patch.object(module, "_call_rpc", return_value={"ok": False, "error": "Go2 unreachable"})
    with client.websocket_connect(
        f"/api/teleop?token={module._csrf_token}", headers={"Origin": "http://testserver"}
    ) as socket:
        assert socket.receive_json() == {"error": "Go2 unreachable"}
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    transport.start.assert_not_called()
    transport.publish.assert_not_called()


async def test_tagging_api_passes_boolean_and_rejects_invalid_toggle(client, module, mocker):
    call = mocker.patch.object(
        module, "_call_rpc", return_value={"ok": True, "result": {"enabled": False}}
    )
    response = await client.post("/api/tagging", json={"enabled": False})
    assert response.json()["result"]["enabled"] is False
    assert call.call_args.args[0].method == "set_automatic_tagging"
    assert call.call_args.args[1] == {"enabled": False}
    response = await client.post("/api/tagging", json={"enabled": "false"})
    assert response.json()["ok"] is False
    assert call.call_count == 1


async def test_auto_speech_queues_only_final_unique_agent_replies(module):
    module._console_loop = asyncio.get_running_loop()
    module._speaker_enabled = True
    module._on_agent(AIMessage(content="First"))
    module._on_agent(AIMessage(content="Second", id="reply-2"))
    module._on_agent(AIMessage(content="Second", id="reply-2"))
    module._on_agent(ToolMessage(content="Tool result", tool_call_id="call"))
    module._on_agent(
        AIMessage(content="Planning", tool_calls=[{"name": "observe", "args": {}, "id": "call"}])
    )
    await asyncio.sleep(0)
    assert module._speech_queue.qsize() == 2
    assert module._speech_queue.get_nowait()[0] == "First"
    assert module._speech_queue.get_nowait()[0] == "Second"
    module._console_loop = None


async def test_arrival_is_announced_once_per_goal_and_cancel_is_not_spoken(module, mocker):
    module._console_loop = asyncio.get_running_loop()
    module._speaker_enabled = True
    module._speaker_generation = 7
    emit = mocker.patch.object(module, "_emit")
    module._on_navigation_state(String("PGO correction: navigation replanned"))
    module._on_navigation_state(String("Navigation cancelled"))
    module._on_navigation_state(String("Arrived within 1 m of target"))
    await asyncio.sleep(0)
    assert module._speech_queue.get_nowait() == ("Woof! We've arrived!", 7)
    emit.assert_any_call(
        {"type": "message", "role": "agent", "content": "Woof! We've arrived!", "tool_calls": []}
    )
    module._on_navigation_state(String("Arrived at target"))
    await asyncio.sleep(0)
    assert module._speech_queue.get_nowait() == ("Woof! We've arrived!", 7)
    module._on_navigation_state(String("Nearby threshold reached; searching for tag image"))
    module._on_navigation_state(String("Visual search timed out; no matching tag view"))
    await asyncio.sleep(0)
    assert module._speech_queue.empty()
    module._on_navigation_state(String("Arrived: tag image matched"))
    await asyncio.sleep(0)
    assert module._speech_queue.get_nowait() == ("Woof! We've arrived!", 7)
    module._speaker_enabled = False
    module._on_navigation_state(String("Arrived within 1 m of target"))
    await asyncio.sleep(0)
    assert module._speech_queue.empty()
    module._console_loop = None


async def test_navigation_distance_api_passes_valid_radius_and_rejects_invalid_values(
    client, module, mocker
):
    call = mocker.patch.object(
        module, "_call_rpc", return_value={"ok": True, "result": {"distance_m": 1.7}}
    )
    result = await client.post("/api/navigation-distance", json={"distance_m": 1.7})
    assert result.json() == {"ok": True, "result": {"distance_m": 1.7}}
    assert call.call_args.args[0].method == "set_nearby_arrival_distance"
    assert call.call_args.args[1] == {"distance_m": 1.7}
    for distance in (True, "1.0", 0.2, 3.1, None):
        result = await client.post("/api/navigation-distance", json={"distance_m": distance})
        assert result.json()["ok"] is False
    assert call.call_count == 1


async def test_visual_search_api_requires_rotation_consent_and_passes_switch(
    client, module, mocker
):
    call = mocker.patch.object(
        module,
        "_call_rpc",
        return_value={"ok": True, "result": {"enabled": True, "searching": False}},
    )
    response = await client.post("/api/visual-arrival", json={"enabled": True})
    assert response.json()["ok"] is False
    call.assert_not_called()
    response = await client.post("/api/visual-arrival", json={"enabled": True, "confirmed": True})
    assert response.json()["result"]["enabled"] is True
    assert call.call_args.args[0].method == "configure_visual_arrival"
    assert call.call_args.args[1] == {"enabled": True}


def test_puppy_events_show_transcript_reply_and_errors_without_double_speech(module, mocker):
    emit = mocker.patch.object(module, "_emit")
    module._speaker_enabled = True
    module._on_puppy({"role": "user", "content": "Hello Puppy"})
    module._on_puppy({"role": "agent", "content": "Woof! Hello!"})
    module._on_puppy({"role": "tool", "content": "Microphone unavailable"})
    assert [call.args[0]["type"] for call in emit.call_args_list] == ["message", "message", "tool"]
    assert module._speech_queue.empty()
    module._speaker_enabled = False
    module._on_puppy({"role": "agent", "content": "Murmur reply", "source": "Puppy · Murmur"})
    assert emit.call_count == 4
    assert emit.call_args.args[0]["source"] == "Puppy · Murmur"
    assert module._speech_queue.empty()


async def test_tools_and_status(client: httpx.AsyncClient) -> None:
    tools = await client.get("/api/tools")
    assert tools.status_code == 200
    assert len(tools.json()) > 5

    status = await client.get("/api/status")
    assert status.status_code == 200
    assert "agent_idle" in status.json()


# ---------------------------------------------------------------------------
# action dispatch (RPC + MCP) — monkey-patched, offline
# ---------------------------------------------------------------------------
async def test_action_rpc(monkeypatch: pytest.MonkeyPatch, module: RobotConsoleModule) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_rpc(op: Any, args: Any) -> dict[str, Any]:
        calls.append((f"{module.config.map_module}/{op.method}", args))
        return {"ok": True, "result": "confirmed"}

    monkeypatch.setattr(module, "_call_rpc", fake_rpc)
    transport = httpx.ASGITransport(app=module._build_app())
    async with httpx.AsyncClient(transport=transport, base_url=f"http://localhost:{PORT}") as c:
        r = await c.post(
            "/api/action", json={"name": "confirm_alignment", "args": {}, "confirmed": True}
        )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert calls and calls[0][0] == "PersistentGo2Map/confirm_alignment"


async def test_action_mcp(monkeypatch: pytest.MonkeyPatch, module: RobotConsoleModule) -> None:
    seen: dict[str, Any] = {}

    async def fake_mcp(op: Any, args: Any) -> dict[str, Any]:
        seen["op"] = op.key
        seen["args"] = args
        return {"ok": True, "result": "tagged the object"}

    monkeypatch.setattr(module, "_call_mcp", fake_mcp)
    transport = httpx.ASGITransport(app=module._build_app())
    async with httpx.AsyncClient(transport=transport, base_url=f"http://localhost:{PORT}") as c:
        r = await c.post(
            "/api/action", json={"name": "tag_object", "args": {"object_name": "coffee table"}}
        )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert seen["op"] == "tag_object"
    assert seen["args"] == {"object_name": "coffee table"}


async def test_action_unknown(monkeypatch: pytest.MonkeyPatch, module: RobotConsoleModule) -> None:
    transport = httpx.ASGITransport(app=module._build_app())
    async with httpx.AsyncClient(transport=transport, base_url=f"http://localhost:{PORT}") as c:
        r = await c.post("/api/action", json={"name": "does_not_exist"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


# ---------------------------------------------------------------------------
# chat -> human input
# ---------------------------------------------------------------------------
async def test_chat_publishes_human_input(
    monkeypatch: pytest.MonkeyPatch, module: RobotConsoleModule
) -> None:
    sent: dict[str, str] = {}
    monkeypatch.setattr(module, "_publish_human_input", lambda text: sent.__setitem__("text", text))
    transport = httpx.ASGITransport(app=module._build_app())
    async with httpx.AsyncClient(transport=transport, base_url=f"http://localhost:{PORT}") as c:
        r = await c.post("/api/chat", json={"message": "go to the kitchen"})
    assert r.json()["ok"] is True
    assert sent["text"] == "go to the kitchen"


async def test_chat_empty_rejected(module: RobotConsoleModule) -> None:
    transport = httpx.ASGITransport(app=module._build_app())
    async with httpx.AsyncClient(transport=transport, base_url=f"http://localhost:{PORT}") as c:
        r = await c.post("/api/chat", json={"message": "   "})
    assert r.json()["ok"] is False


# ---------------------------------------------------------------------------
# live event stream (initial status frame)
# ---------------------------------------------------------------------------
async def test_event_stream_initial_status(module: RobotConsoleModule) -> None:
    # The module's ``_build_app`` is exercised through a *real* (async) server
    # rather than httpx.ASGITransport, because the latter does not stream a
    # ``StreamingResponse`` incrementally (it would block until the SSE
    # generator's 15 s keep-alive fires). A real server flushes the first
    # frame immediately, which is what the front-end relies on.
    module._status.update({"agent_idle": True, "navigation_ready": True})
    app = module._build_app()
    cfg = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
    server = uvicorn.Server(cfg)
    loop = asyncio.new_event_loop()

    def _run() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.serve())
        finally:
            loop.close()

    thread = threading.Thread(target=_run, name="console-test-uvicorn", daemon=True)
    thread.start()
    try:
        await asyncio.wait_for(_wait_for_http(f"http://127.0.0.1:{PORT}/"), timeout=10.0)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}") as c:
            got = False
            try:
                async with c.stream("GET", "/events") as resp:
                    assert resp.status_code == 200
                    async for line in resp.aiter_lines():
                        if "event: status" in line:
                            got = True
                            break
            except asyncio.TimeoutError:
                pass
        assert got, "did not receive initial status frame"
    finally:
        server.should_exit = True
        thread.join(timeout=3.0)
        assert not thread.is_alive()


async def _wait_for_http(url: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            async with httpx.AsyncClient(base_url=url, timeout=1.0) as c:
                await c.get("/")
            return
        except Exception:
            await asyncio.sleep(0.1)
    raise TimeoutError(url)


# ---------------------------------------------------------------------------
# message normalization (unit)
# ---------------------------------------------------------------------------
def test_normalize_message_shapes() -> None:
    assert _normalize_message({"role": "user", "content": "hi"})["kind"] == "user"
    assert _normalize_message({"role": "agent", "content": "yo"})["kind"] == "agent"
    tool = _normalize_message({"role": "tool", "name": "tag_object", "content": "ok"})
    assert tool["kind"] == "tool"
    assert tool["name"] == "tag_object"

    calls = _normalize_message(
        {
            "role": "agent",
            "content": "",
            "tool_calls": [{"name": "navigate_to_memory_tag", "args": {"tag": "x"}}],
        }
    )
    assert calls["kind"] == "agent"
    assert calls["tool_calls"][0]["name"] == "navigate_to_memory_tag"


def test_content_to_text_variants() -> None:
    assert _content_to_text("abc") == "abc"
    assert _content_to_text([{"text": "a"}, {"text": "b"}]) == "a\nb"
    assert _content_to_text("") == ""
    assert _content_to_text(None) == ""


@pytest.mark.parametrize(
    "key",
    [
        "tag_object",
        "tag_location",
        "query_memory_tags",
        "navigate_to_memory_tag",
        "stop_navigation",
    ],
)
def test_console_arguments_match_real_skill_signatures(key):
    schema = OPERATIONS[key].schema
    args = {name: "office" for name in schema.get("properties", {})}
    bound = inspect.signature(getattr(NavigationSkillContainer, key)).bind(None, **args)
    assert dict(bound.arguments) == {"self": None, **args}


@pytest.mark.parametrize("key", [key for key, op in OPERATIONS.items() if op.human_only])
async def test_human_operations_require_confirmation(client, module, mocker, key):
    rpc = mocker.patch.object(module, "_call_rpc", new_callable=mocker.AsyncMock)
    mcp = mocker.patch.object(module, "_call_mcp", new_callable=mocker.AsyncMock)
    result = (await client.post("/api/action", json={"name": key})).json()
    assert result == {"ok": False, "error": "Human confirmation is required"}
    rpc.assert_not_called()
    mcp.assert_not_called()


@pytest.mark.parametrize(
    "args", [{"tag": "office"}, {}, {"object_name": ""}, {"object_name": 12}, []]
)
async def test_invalid_arguments_never_reach_mcp(client, module, mocker, args):
    call = mocker.patch.object(module, "_call_mcp", new_callable=mocker.AsyncMock)
    result = (await client.post("/api/action", json={"name": "tag_object", "args": args})).json()
    assert result["ok"] is False
    call.assert_not_called()


async def test_chat_transport_error_is_not_success(client, mocker):
    mocker.patch("dimos.web.console.module.make_transport", side_effect=RuntimeError("offline"))
    response = await client.post("/api/chat", json={"message": "hello"})
    assert response.json() == {"ok": False, "error": "Chat transport failed; message was not sent"}


@pytest.mark.parametrize(
    "payload",
    [
        {"result": {"content": [{"text": "depth missing"}], "isError": True}},
        {"result": {"content": [{"text": "Error running tool 'tag_object': invalid args"}]}},
        {"result": {"content": [{"text": "Tool not found: tag_object"}]}},
        {"error": {"message": "bad request"}},
        {"result": {}},
        [],
    ],
)
async def test_mcp_failure_is_reported(module, mocker, payload):
    client = mocker.AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(
        200, json=payload, request=httpx.Request("POST", "http://localhost/mcp")
    )
    mocker.patch("dimos.web.console.module.httpx.AsyncClient", return_value=client)
    result = await module._call_mcp(OPERATIONS["tag_object"], {"object_name": "chair"})
    assert result["ok"] is False
    assert result["error"]


async def test_status_refresh_is_cached_without_overwriting_live_idle(module, mocker):
    module._status["agent_idle"] = True
    mocker.patch.object(
        module,
        "_run_blocking",
        new_callable=mocker.AsyncMock,
        return_value={"navigation_ready": True, "alignment_status": "Ready:", "agent_idle": False},
    )
    status = await module._refresh_status()
    assert status["navigation_ready"] is True
    assert module._status["navigation_ready"] is True
    assert module._status["agent_idle"] is True


def test_message_serialization_preserves_tool_call_ids_and_args():
    message = AIMessage(
        content="",
        tool_calls=[{"id": "call-1", "name": "query_memory_tags", "args": {"query": "office"}}],
    )
    payload = serialize_message(message)
    assert payload["tool_calls"] == [
        {"id": "call-1", "name": "query_memory_tags", "args": {"query": "office"}}
    ]
    result = serialize_message(
        ToolMessage(content="found", tool_call_id="call-1", name="query_memory_tags")
    )
    assert result["tool_call_id"] == "call-1"
    assert result["content"] == "found"
