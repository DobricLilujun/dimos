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
import threading
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import uvicorn

from dimos.web.console.frontend import INDEX_HTML
from dimos.web.console.module import RobotConsoleModule, _normalize_message


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
        try:
            m._close_module()
        except Exception:
            pass


@pytest.fixture()
async def client(module: RobotConsoleModule) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=module._build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url=f"http://localhost:{PORT}"
    ) as c:
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
    assert data["rerun_url"].endswith("9878")
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
async def test_action_rpc(
    monkeypatch: pytest.MonkeyPatch, module: RobotConsoleModule
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_rpc(op: Any, args: Any) -> dict[str, Any]:
        calls.append((f"{module.config.map_module}/{op.method}", args))
        return {"ok": True, "result": "confirmed"}

    monkeypatch.setattr(module, "_call_rpc", fake_rpc)
    transport = httpx.ASGITransport(app=module._build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url=f"http://localhost:{PORT}"
    ) as c:
        r = await c.post(
            "/api/action", json={"name": "confirm_alignment", "args": {}}
        )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert calls and calls[0][0] == "PersistentGo2Map/confirm_alignment"


async def test_action_mcp(
    monkeypatch: pytest.MonkeyPatch, module: RobotConsoleModule
) -> None:
    seen: dict[str, Any] = {}

    async def fake_mcp(op: Any, args: Any) -> dict[str, Any]:
        seen["op"] = op.key
        seen["args"] = args
        return {"ok": True, "result": "tagged the object"}

    monkeypatch.setattr(module, "_call_mcp", fake_mcp)
    transport = httpx.ASGITransport(app=module._build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url=f"http://localhost:{PORT}"
    ) as c:
        r = await c.post(
            "/api/action", json={"name": "tag_object", "args": {"tag": "coffee table"}}
        )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert seen["op"] == "tag_object"
    assert seen["args"] == {"tag": "coffee table"}


async def test_action_unknown(
    monkeypatch: pytest.MonkeyPatch, module: RobotConsoleModule
) -> None:
    transport = httpx.ASGITransport(app=module._build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url=f"http://localhost:{PORT}"
    ) as c:
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
    monkeypatch.setattr(
        module, "_publish_human_input", lambda text: sent.__setitem__("text", text)
    )
    transport = httpx.ASGITransport(app=module._build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url=f"http://localhost:{PORT}"
    ) as c:
        r = await c.post("/api/chat", json={"message": "go to the kitchen"})
    assert r.json()["ok"] is True
    assert sent["text"] == "go to the kitchen"


async def test_chat_empty_rejected(module: RobotConsoleModule) -> None:
    transport = httpx.ASGITransport(app=module._build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url=f"http://localhost:{PORT}"
    ) as c:
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
    cfg = uvicorn.Config(
        app, host="127.0.0.1", port=PORT, log_level="error"
    )
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
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{PORT}"
        ) as c:
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
        time.sleep(0.3)
        thread.join(timeout=3.0)


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
    tool = _normalize_message(
        {"role": "tool", "name": "tag_object", "content": "ok"}
    )
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
    from dimos.web.console.module import _content_to_text

    assert _content_to_text("abc") == "abc"
    assert _content_to_text([{"text": "a"}, {"text": "b"}]) == "a\nb"
    assert _content_to_text("") == ""
    assert _content_to_text(None) == ""