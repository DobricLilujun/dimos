"""
Live smoke test for RobotConsoleModule.

Boots the *real* module (``start()`` -> real uvicorn server) with the agent
streams stubbed (no live agent needed in a smoke test), then drives the
console over real HTTP + a real SSE connection to prove the UI and the
event stream work end-to-end without the full robot stack.
"""

import time
import urllib.request

import httpx
import pytest

from dimos.web.console.module import RobotConsoleModule

PORT = 8091
MCP_PORT = 9990
RERUN_WEB_PORT = 9878


def _wait_server(base: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/api/status", timeout=2.0)
            return
        except Exception:
            time.sleep(0.2)
    raise AssertionError("server did not come up")


def _run() -> None:
    m = RobotConsoleModule(port=PORT, mcp_port=MCP_PORT, rerun_web_port=RERUN_WEB_PORT)
    # stub the agent/idle/tool subscriptions: no live agent in a smoke test,
    # but keep the real HTTP server + real SSE.
    m._setup_agent_streams = lambda: None  # type: ignore[assignment]
    m._teardown_agent_streams = lambda: None  # type: ignore[assignment]

    m.start()
    try:
        base = f"http://127.0.0.1:{PORT}"
        _wait_server(base)

        # 1) the UI HTML is served and contains the SEDAN branding + controls.
        html = httpx.get(base + "/", timeout=5.0).text
        for needle in ("sedan", "chat", "rerun"):
            assert needle in html.lower(), f"missing {needle!r} in UI"
        assert "/events" in html, "SSE wiring missing"
        print(f"OK: index served ({len(html)} bytes); SEDAN + chat + rerun present")

        # 2) config endpoint exposes rerun url + operations.
        cfg = httpx.get(base + "/api/config", timeout=5.0).json()
        assert cfg["rerun_url"] == f"http://127.0.0.1:{RERUN_WEB_PORT}", cfg
        assert cfg["mcp_url"] == f"http://127.0.0.1:{MCP_PORT}/mcp", cfg
        assert len(cfg["operations"]) > 0
        print(f"OK: /api/config -> {len(cfg['operations'])} operations, rerun_url ok")

        # 3) tools + status are served.
        tools = httpx.get(base + "/api/tools", timeout=5.0).json()
        assert len(tools) == len(cfg["operations"]), "tools vs operations mismatch"
        status = httpx.get(base + "/api/status", timeout=5.0).json()
        assert isinstance(status, dict), status
        print(f"OK: /api/tools ({len(tools)}); /api/status ok")

        # 4) chat: empty rejected, real accepted + echoed.
        empty = httpx.post(base + "/api/chat", json={"message": ""}, timeout=5.0).json()
        assert empty["ok"] is False, empty
        chat = httpx.post(base + "/api/chat", json={"message": "hello robot"}, timeout=5.0).json()
        assert chat["ok"] is True and chat["echo"] == "hello robot", chat
        print("OK: /api/chat empty rejected, real message accepted + echoed")

        # 5) action: unknown op rejected.
        bad = httpx.post(base + "/api/action", json={"name": "nope"}, timeout=5.0).json()
        assert bad["ok"] is False, bad
        print("OK: /api/action unknown-op rejected")

        # 6) SSE initial status frame over a real socket.
        with httpx.stream("GET", base + "/events", timeout=8.0) as stream:
            chunks: list[str] = []
            for line in stream.iter_lines():
                chunks.append(line)
                if any(c.startswith("data:") for c in chunks):
                    break
        joined = "\n".join(chunks)
        assert "data:" in joined and "status" in joined, joined[:400]
        print("OK: /events initial status frame over real socket")

        print("ALL SMOKE CHECKS PASSED")
    finally:
        try:
            m.stop()
        except Exception:
            pass


def test_console_live_smoke() -> None:
    _run()