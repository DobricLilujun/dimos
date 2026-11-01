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

"""Real HTTP/SSE smoke test without robot, transport side effects or LLM."""

import time

import httpx
import pytest

from dimos.web.console.module import RobotConsoleModule


@pytest.fixture
def live_console(mocker):
    module = RobotConsoleModule(port=0)
    mocker.patch.object(module, "_setup_agent_streams")
    mocker.patch.object(module, "_teardown_agent_streams")
    publish = mocker.patch.object(module, "_publish_human_input")
    module.start()
    try:
        deadline = time.monotonic() + 10
        while not module._server.started:
            if time.monotonic() >= deadline:
                raise AssertionError("Console server did not start")
            module._server_thread.join(timeout=0.01)
        port = module._server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}", publish
    finally:
        module.stop()


def test_console_live_smoke(live_console):
    base, publish = live_console
    with httpx.Client(base_url=base, timeout=5) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "SEDAN GROUP" in response.text
        assert "settings-dialog" in response.text
        cfg = client.get("/api/config").json()
        assert cfg["rerun_url"] == (
            "http://127.0.0.1:9878/?url=rerun%2Bhttp%3A%2F%2F127.0.0.1%3A9877%2Fproxy"
        )
        assert len(client.get("/api/tools").json()) == len(cfg["operations"])
        assert client.post("/api/chat", json={"message": ""}).json()["ok"] is False
        assert client.post("/api/chat", json={"message": "hello robot"}).json() == {
            "ok": True,
            "echo": "hello robot",
        }
        publish.assert_called_once_with("hello robot")
        assert client.post("/api/action", json={"name": "nope"}).json()["ok"] is False
        with client.stream("GET", "/events") as stream:
            first = next(line for line in stream.iter_lines() if line.startswith("data:"))
        assert '"agent_idle": null' in first
