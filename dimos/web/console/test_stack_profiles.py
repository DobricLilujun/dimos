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

"""Stack profiles: the console launching blueprints other than the persistent one.

Offline like the other console tests: no robot, no simulator, no cloud calls.
"""

from __future__ import annotations

from collections.abc import Iterator
import io
import socket
import subprocess
import sys
import threading
import time

import httpx
from pydantic import ValidationError
import pytest

from dimos.core.coordination.blueprint_config.parser import BlueprintConfigParser
from dimos.robot.all_blueprints import all_blueprints
from dimos.robot.unitree.go2.blueprints.agentic.demo_unitree_go2_agentic_person_following import (
    demo_unitree_go2_agentic_person_following,
)
from dimos.robot.unitree.go2.blueprints.agentic.unitree_go2_agentic_persistent_console import (
    unitree_go2_agentic_persistent_demo,
)
from dimos.robot.unitree.go2.blueprints.smart.demo_unitree_go2_dynamic_goal import (
    demo_unitree_go2_dynamic_goal,
)
from dimos.robot.unitree.go2.blueprints.smart.unitree_go2 import unitree_go2
from dimos.web.console.module import OPERATIONS, RobotConsoleModule
from dimos.web.console.profiles import PROFILES
from dimos.web.console.settings import (
    SIM_SCENES_KEPT,
    ConsoleRuntime,
    ConsoleSettings,
)

PORT = 8192

BLUEPRINTS = {
    "persistent": unitree_go2_agentic_persistent_demo,
    "go2": unitree_go2,
    "dynamic-goal": demo_unitree_go2_dynamic_goal,
    "person-following": demo_unitree_go2_agentic_person_following,
}
ALL_CHOICES = [
    (key, connection) for key, profile in PROFILES.items() for connection in profile.connections
]


def _settings(profile: str, connection: str = "simulation", **updates: object) -> ConsoleSettings:
    return ConsoleSettings(
        profile=profile,
        replay=connection == "replay",
        simulation=connection == "simulation",
        **updates,  # type: ignore[arg-type]
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("UNITREE_AES_128_KEY", raising=False)
    return ConsoleRuntime(tmp_path, tmp_path / "console.json")


@pytest.fixture
def process(runtime, mocker):
    child = mocker.Mock()
    child.pid = 123456
    child.poll.return_value = None
    popen = mocker.patch("dimos.web.console.settings.subprocess.Popen", return_value=child)
    mocker.patch("dimos.web.console.settings.threading.Thread")
    mocker.patch.object(runtime, "_ports_available")
    child.popen = popen
    return child


# ---------------------------------------------------------------------------
# the profile list itself
# ---------------------------------------------------------------------------
def test_every_profile_names_a_registered_blueprint():
    for profile in PROFILES.values():
        assert profile.blueprint in all_blueprints, profile.key


def test_every_listed_operation_exists():
    for profile in PROFILES.values():
        for operation in profile.operations or ():
            assert operation in OPERATIONS, (profile.key, operation)


def test_a_stack_without_an_agent_has_nothing_to_call():
    for profile in PROFILES.values():
        if not profile.has_agent:
            assert not profile.operations and not profile.ready_tools, profile.key


def test_the_default_stack_is_unchanged():
    settings = ConsoleSettings()
    argv = settings.argv()

    assert settings.profile == "persistent"
    assert settings.connection == "robot"
    assert argv[4] == "unitree-go2-agentic-persistent-demo"
    assert not any(arg.startswith("--simulation") for arg in argv)


# ---------------------------------------------------------------------------
# settings: what is allowed, and the launch command
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("key", "connection"), ALL_CHOICES)
def test_launch_command_parses_against_the_real_blueprint(key, connection):
    settings = _settings(key, connection)
    argv = settings.argv()

    assert argv[4] == PROFILES[key].blueprint
    parsed = BlueprintConfigParser(BLUEPRINTS[key]).parse(argv[5:], environ={})
    assert parsed.global_config["simulation"] == ("mujoco" if connection == "simulation" else "")
    assert parsed.global_config["replay"] is (connection == "replay")


def test_simulation_is_mujoco_and_replay_is_switched_off():
    argv = _settings("go2", "simulation").argv()

    assert "--simulation=mujoco" in argv
    assert "--replay=false" in argv


@pytest.mark.parametrize("key", ["go2", "dynamic-goal"])
def test_stacks_without_an_agent_get_no_agent_or_map_flags(key):
    argv = _settings(key).argv()

    for prefix in ("--persistentgo2", "--mcpclient", "--spatialmemory", "--mcp-port", "--go2conn"):
        assert not any(arg.startswith(prefix) for arg in argv), prefix
    assert any(arg.startswith("--rerunbridgemodule.web-port=") for arg in argv)


def test_the_person_following_stack_keeps_its_own_prompt_and_gets_the_vision_model():
    settings = _settings("person-following", vlm_model="local-vision", agent_model="local-agent")
    argv = settings.argv()

    assert "--mcpclient.model=local-agent" in argv
    assert "--personnavigationskillcontainer.vlm-model=local-vision" in argv
    assert not any(arg.startswith("--mcpclient.system-prompt") for arg in argv)
    assert not any(arg.startswith("--persistentgo2") for arg in argv)


def test_replay_and_simulation_cannot_both_be_on():
    with pytest.raises(ValidationError, match="cannot both be on"):
        ConsoleSettings(replay=True, simulation=True)


@pytest.mark.parametrize("connection", ["robot", "replay"])
def test_a_simulation_only_demo_refuses_other_connections(connection):
    for key in ("dynamic-goal", "person-following"):
        with pytest.raises(ValidationError, match="cannot run with"):
            _settings(key, connection)


def test_an_unknown_stack_is_refused():
    with pytest.raises(ValidationError, match="Unknown stack"):
        ConsoleSettings(profile="rm -rf")


def test_settings_saved_before_profiles_existed_still_load(tmp_path):
    path = tmp_path / "console.json"
    path.write_text('{"robot_ip": "192.168.63.218", "replay": true}')

    runtime = ConsoleRuntime(tmp_path, path)

    assert runtime.settings.profile == "persistent"
    assert runtime.settings.connection == "replay"
    assert runtime.profile.has_map


# ---------------------------------------------------------------------------
# runtime: starting, scenes, ports
# ---------------------------------------------------------------------------
def test_a_stack_without_a_map_starts_without_any_scene(runtime, process):
    # A scene path that would be refused for a persistent stack must not matter here.
    runtime.settings = _settings("go2", "replay", scene_map_dir=str(runtime.project_dir))

    plan = runtime.prepare_start()
    runtime.start()

    assert plan["confirm_new_map"] is False and plan["overwrite_required"] is False
    assert process.popen.call_args.args[0][4] == "unitree-go2"
    assert runtime.status()["state"] == "starting"
    assert runtime.profile.key == "go2"


def test_a_simulated_persistent_session_builds_a_new_map_in_a_fresh_folder(runtime, process):
    saved = runtime.project_dir / "real_office"
    saved.mkdir()
    (saved / "map.pc2.lcm").write_bytes(b"saved map")
    runtime.settings = _settings(
        "persistent", "simulation", map_mode="restore", scene_map_dir=str(saved)
    )

    plan = runtime.prepare_start()
    runtime.start()

    argv = process.popen.call_args.args[0]
    config = (
        BlueprintConfigParser(unitree_go2_agentic_persistent_demo)
        .parse(argv[5:], environ={})
        .module_kwargs("persistentgo2map")
    )
    scene = runtime.sim_scene_root.glob("*")
    (scene,) = list(scene)
    assert plan["confirm_new_map"] is False and plan["overwrite_required"] is False
    assert config["create_new"] is True
    assert config["manual_capture"] is False and config["startup_rotation"] is False
    assert config["map_file"] == str(scene / "map.pc2.lcm")
    assert scene.is_dir() and not any(scene.iterdir())
    # The saved scene is left alone, and the saved settings still say Restore.
    assert (saved / "map.pc2.lcm").read_bytes() == b"saved map"
    assert runtime.settings.map_mode == "restore"


def test_each_simulated_start_gets_its_own_scene(runtime, process):
    runtime.settings = _settings("persistent", "simulation")
    runtime.start()
    process.poll.return_value = 0
    runtime.start()

    assert len(list(runtime.sim_scene_root.iterdir())) == 2


def test_only_the_newest_simulated_scenes_are_kept(runtime):
    root = runtime.sim_scene_root
    root.mkdir(parents=True)
    old = [f"20260101-0000{n:02d}-aaaaaa" for n in range(7)]
    for name in old:
        (root / name).mkdir()
    (root / "keep-me").mkdir()
    (root / "20260101-notes.txt").write_text("not a scene")

    fresh = runtime._new_sim_scene()

    kept = sorted(path.name for path in root.iterdir() if path.is_dir())
    assert len([name for name in kept if name != "keep-me"]) == SIM_SCENES_KEPT
    assert fresh.name in kept
    assert old[-1] in kept and old[0] not in kept
    assert "keep-me" in kept and (root / "20260101-notes.txt").exists()


def test_the_mcp_port_only_matters_to_a_stack_that_has_an_mcp_server(runtime, mocker, tmp_path):
    (tmp_path / "runs").mkdir()
    mocker.patch("dimos.web.console.settings.REGISTRY_DIR", tmp_path / "runs")
    mcp, web, grpc = _free_port(), _free_port(), _free_port()
    ports = {"mcp_port": mcp, "rerun_web_port": web, "rerun_grpc_port": grpc}
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", mcp))
        runtime.settings = _settings("go2", "simulation", **ports)
        runtime._ports_available()
        runtime.settings = _settings("persistent", "simulation", **ports)
        with pytest.raises(ValueError, match="occupied"):
            runtime._ports_available()


# ---------------------------------------------------------------------------
# runtime: when is the stack ready
# ---------------------------------------------------------------------------
def _wait_for(condition, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    waiter = threading.Event()
    while time.monotonic() < deadline:
        if condition():
            return True
        waiter.wait(0.1)
    return False


@pytest.fixture
def real_child(runtime) -> Iterator[subprocess.Popen[str]]:
    children: list[subprocess.Popen[str]] = []

    def launch(code: str) -> subprocess.Popen[str]:
        child = subprocess.Popen(
            [sys.executable, "-u", "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        children.append(child)
        runtime._process = child
        return child

    yield launch  # type: ignore[misc]
    runtime._halt.set()
    for child in children:
        child.kill()
        child.wait(timeout=10)
        if child.stdout is not None:
            child.stdout.close()
    if runtime._monitor is not None:
        runtime._monitor.join(timeout=10)


def test_a_stack_without_an_agent_is_ready_when_the_blueprint_reports_it_started(
    runtime, real_child
):
    runtime.settings = _settings("go2", "replay")
    runtime._launch_profile = runtime.settings.stack
    runtime._started_seen = False
    runtime._state = "starting"
    real_child(
        "import time; print('loading', flush=True); time.sleep(0.5); "
        "print('12:00:00.000 [inf][module_coordinator.py] Blueprint started build_s=1', flush=True);"
        "time.sleep(60)"
    )
    runtime._monitor = threading.Thread(target=runtime._watch, daemon=True)
    runtime._monitor.start()

    assert _wait_for(lambda: runtime.status()["state"] == "running")
    assert runtime.ready


def test_a_stack_that_has_not_reported_started_is_not_ready(runtime, real_child):
    runtime.settings = _settings("go2", "replay")
    runtime._launch_profile = runtime.settings.stack
    runtime._started_seen = False
    runtime._state = "starting"
    real_child("import time; print('still loading', flush=True); time.sleep(60)")
    runtime._monitor = threading.Thread(target=runtime._watch, daemon=True)
    runtime._monitor.start()

    assert _wait_for(lambda: any("still loading" in line for line in runtime.logs()))
    threading.Event().wait(1.2)  # more than two monitor cycles
    assert runtime.status()["state"] == "starting"


@pytest.mark.parametrize(
    ("key", "listed", "ready"),
    [
        ("persistent", ["tag_object", "query_memory_tags", "stop_navigation"], True),
        ("persistent", ["tag_object", "query_memory_tags"], False),
        ("person-following", ["describe_visible_people", "follow_person_with_planner"], False),
        (
            "person-following",
            ["describe_visible_people", "follow_person_with_planner", "stop_navigation"],
            True,
        ),
    ],
)
def test_an_agent_stack_is_ready_when_its_own_tools_are_listed(runtime, mocker, key, listed, ready):
    runtime.settings = _settings(key, "simulation" if key == "person-following" else "replay")
    runtime._launch_profile = runtime.settings.stack
    runtime._state = "starting"
    response = mocker.Mock()
    response.json.return_value = {"result": {"tools": [{"name": name} for name in listed]}}
    mocker.patch("dimos.web.console.settings.requests.post", return_value=response)
    process = mocker.Mock()
    process.poll.return_value = None
    process.stdout = io.StringIO("")
    runtime._process = process
    runtime._monitor = threading.Thread(target=runtime._watch, daemon=True)
    runtime._monitor.start()
    try:
        if ready:
            assert _wait_for(lambda: runtime.status()["state"] == "running")
        else:
            assert _wait_for(lambda: response.json.called)
            threading.Event().wait(0.7)
            assert runtime.status()["state"] == "starting"
    finally:
        runtime._halt.set()
        runtime._monitor.join(timeout=10)


# ---------------------------------------------------------------------------
# the console app: what each stack shows and accepts
# ---------------------------------------------------------------------------
@pytest.fixture()
def module(monkeypatch: pytest.MonkeyPatch) -> Iterator[RobotConsoleModule]:
    m = RobotConsoleModule(port=PORT, mcp_port=9990, rerun_web_port=9878)
    monkeypatch.setattr(m, "_setup_agent_streams", lambda: None)
    monkeypatch.setattr(m, "_teardown_agent_streams", lambda: None)
    try:
        yield m
    finally:
        m._close_module()


def _client(module: RobotConsoleModule) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=module._build_app()),
        base_url=f"http://localhost:{PORT}",
        headers={"x-console-token": module._csrf_token},
    )


def _attach(module: RobotConsoleModule, tmp_path, key: str, connection: str) -> ConsoleRuntime:
    module.runtime = ConsoleRuntime(tmp_path, tmp_path / "console.json")
    module.runtime.settings = _settings(key, connection)
    module.runtime._state = "running"
    return module.runtime


async def test_the_persistent_stack_shows_every_operation(module, tmp_path):
    _attach(module, tmp_path, "persistent", "replay")
    async with _client(module) as client:
        config = (await client.get("/api/config")).json()

    assert [op["key"] for op in config["operations"]] == list(OPERATIONS)
    assert config["profile"]["has_map"] and config["profile"]["has_agent"]
    assert [item["key"] for item in config["profiles"]] == list(PROFILES)


async def test_a_plain_stack_shows_no_agent_or_map_operations(module, tmp_path):
    _attach(module, tmp_path, "go2", "simulation")
    async with _client(module) as client:
        config = (await client.get("/api/config")).json()
        tools = (await client.get("/api/tools")).json()

    assert config["operations"] == [] and tools == []
    assert not config["profile"]["has_map"] and not config["profile"]["has_agent"]


async def test_the_agent_stack_without_a_map_shows_only_what_works_without_one(module, tmp_path):
    _attach(module, tmp_path, "person-following", "simulation")
    async with _client(module) as client:
        config = (await client.get("/api/config")).json()

    keys = {op["key"] for op in config["operations"]}
    assert keys == set(PROFILES["person-following"].operations or ())
    assert keys.isdisjoint({"confirm_alignment", "save_map", "begin_demo_exploration"})


async def test_an_operation_the_stack_does_not_have_is_refused_by_the_server(
    module, tmp_path, mocker
):
    _attach(module, tmp_path, "go2", "simulation")
    dispatch = mocker.patch.object(module, "_dispatch")
    async with _client(module) as client:
        response = await client.post("/api/action", json={"name": "tag_object", "args": {}})

    assert response.json()["ok"] is False
    assert "not available in this stack" in response.json()["error"]
    dispatch.assert_not_called()


async def test_chat_and_diagnostics_need_an_agent(module, tmp_path, mocker):
    _attach(module, tmp_path, "go2", "simulation")
    publish = mocker.patch.object(module, "_publish_human_input")
    async with _client(module) as client:
        chat = (await client.post("/api/chat", json={"message": "hello"})).json()
        diagnostics = (await client.get("/api/diagnostics")).json()

    assert chat == {"ok": False, "error": "This stack has no agent to chat with"}
    assert diagnostics == {"ok": False, "error": "This stack has no MCP server"}
    publish.assert_not_called()


async def test_status_of_a_stack_without_a_map_does_not_probe_the_map(module, tmp_path, mocker):
    _attach(module, tmp_path, "go2", "simulation")
    probe = mocker.patch("dimos.web.console.module._status_probe")

    status = await module._refresh_status()

    probe.assert_not_called()
    assert status["navigation_ready"] is True
    assert status["alignment_status"] is None and status["fusion_status"] is None


async def test_status_of_a_persistent_stack_still_probes_the_map(module, tmp_path, mocker):
    _attach(module, tmp_path, "persistent", "replay")
    probe = mocker.patch(
        "dimos.web.console.module._status_probe", return_value={"navigation_ready": False}
    )

    await module._refresh_status()

    probe.assert_called_once()


async def test_stopping_a_stack_without_a_map_does_not_ask_for_a_map_save(module, tmp_path, mocker):
    runtime = _attach(module, tmp_path, "go2", "simulation")
    mocker.patch.object(runtime, "status", return_value={"pid": 99, "state": "running"})
    stop = mocker.patch.object(runtime, "stop", return_value={"state": "stopped"})
    rpc = mocker.patch.object(module, "_call_rpc")
    async with _client(module) as client:
        response = await client.post("/api/stack/stop", json={"confirmed": True, "save": True})

    assert response.json()["ok"] is True
    rpc.assert_not_called()
    stop.assert_called_once()


def test_the_page_has_the_stack_selectors():
    from dimos.web.console.frontend import INDEX_HTML

    for element in ("stack-profile", "stack-connection", "stack-map-field", "persistent-controls"):
        assert f'id="{element}"' in INDEX_HTML, element
