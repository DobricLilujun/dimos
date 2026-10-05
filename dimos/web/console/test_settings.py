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

import json
import os
import signal
import subprocess
import sys

from fastapi import FastAPI
import httpx
import psutil
from pydantic import ValidationError
import pytest

from dimos.agents.mcp.mcp_client import McpClient
from dimos.core.coordination.blueprint_config.parser import BlueprintConfigParser
from dimos.mapping.relocalization.go2.persistent import PersistentGo2Planner
from dimos.navigation.experimental.frontier_exploration.demo_explorer import DemoExplorer
from dimos.navigation.experimental.frontier_exploration.wavefront_frontier_goal_selector import (
    WavefrontFrontierExplorer,
)
from dimos.robot.unitree.go2.blueprints.agentic.unitree_go2_agentic_persistent import (
    unitree_go2_agentic_persistent,
)
from dimos.robot.unitree.go2.blueprints.agentic.unitree_go2_agentic_persistent_console import (
    unitree_go2_agentic_persistent_console,
    unitree_go2_agentic_persistent_demo,
)
from dimos.robot.unitree.go2.connection import ConnectionConfig, GO2Connection
from dimos.visualization.rerun.bridge import RerunBridgeModule
from dimos.visualization.rerun.constants import RERUN_GRPC_PORT
from dimos.web.console import __main__ as entrypoint
from dimos.web.console.module import RobotConsoleModule
from dimos.web.console.prompts import CONSOLE_AGENT_PROMPT
from dimos.web.console.settings import ConsoleRuntime, ConsoleSettings, SettingsUpdate


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("UNITREE_AES_128_KEY", raising=False)
    return ConsoleRuntime(tmp_path, tmp_path / "console.json")


@pytest.mark.parametrize(
    "field,value",
    [
        ("robot_ip", "not-an-ip"),
        ("agent_url", "https://user:password@host/v1"),
        ("vlm_url", "file:///etc/passwd"),
        ("vlm_distance_m", -1),
        ("rotation_speed", 2),
        ("rotation_duration", float("nan")),
        ("mcp_port", 0),
        ("object_segmenter", "unknown"),
        ("agent_model", ""),
    ],
)
def test_invalid_settings_are_rejected(field, value):
    with pytest.raises(ValidationError):
        ConsoleSettings.model_validate({field: value})


def test_settings_persist_without_keys_and_reload(runtime):
    (runtime.project_dir / ".env").write_text(
        f"OPENAI_API_KEY=test-only-key\nUNITREE_AES_128_KEY={'0' * 32}\n"
    )
    update = SettingsUpdate(
        settings=ConsoleSettings(robot_ip="192.168.63.218"),
    )
    response = runtime.save(update)
    stored = runtime.settings_path.read_text()
    assert "test-only-key" not in stored
    assert "0" * 32 not in stored
    assert "test-only-key" not in json.dumps(response)
    assert response["secrets"] == {"OPENAI_API_KEY": True, "UNITREE_AES_128_KEY": True}
    assert os.stat(runtime.settings_path).st_mode & 0o777 == 0o600
    restored = ConsoleRuntime(runtime.project_dir, runtime.settings_path)
    assert restored.settings.robot_ip == "192.168.63.218"
    assert restored.public_settings()["secrets"] == {
        "OPENAI_API_KEY": True,
        "UNITREE_AES_128_KEY": True,
    }


def test_settings_reload_keys_from_env_without_modifying_file(runtime, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "inherited-key")
    assert runtime.public_settings()["secrets"]["OPENAI_API_KEY"] is False
    env = runtime.project_dir / ".env"
    env.write_text("OPENAI_API_KEY=test-only\n")
    runtime.save(SettingsUpdate(settings=ConsoleSettings()))
    assert env.read_text() == "OPENAI_API_KEY=test-only\n"
    assert runtime.public_settings()["secrets"]["OPENAI_API_KEY"] is True
    env.write_text("")
    assert runtime.public_settings()["secrets"]["OPENAI_API_KEY"] is False


@pytest.mark.parametrize(
    "mode,capture", [("new", "manual"), ("restore", "manual"), ("restore", "rotation")]
)
def test_settings_command_parses_against_real_blueprint(mode, capture):
    settings = ConsoleSettings(
        map_mode=mode,
        capture_mode=capture,
        replay=True,
        agent_model="openai:local-vllm",
        rerun_grpc_port=9887,
        pgo_enabled=True,
    )
    parsed = BlueprintConfigParser(unitree_go2_agentic_persistent).parse(
        settings.argv()[5:], environ={}
    )
    config = parsed.module_kwargs("persistentgo2map")
    assert config["create_new"] is (mode == "new")
    assert config["manual_capture"] is (mode == "restore" and capture == "manual")
    assert config["startup_rotation"] is (mode == "restore" and capture == "rotation")
    assert config["pgo_enabled"] is True
    assert config["auto_pause_fusion"] is True
    assert config["fusion_resume_speed"] == 0.04
    assert config["fusion_stationary_duration"] == 1.0
    assert parsed.module_kwargs("spatialmemory")["object_segmenter"] == "yolo"
    assert parsed.module_kwargs("rerunbridgemodule")["rerun_web"] is True
    assert (
        parsed.module_kwargs("rerunbridgemodule")["connect_url"]
        == "rerun+http://127.0.0.1:9887/proxy"
    )
    assert parsed.module_kwargs("mcpclient")["model"] == "openai:local-vllm"
    assert parsed.module_kwargs("mcpclient")["system_prompt"] == CONSOLE_AGENT_PROMPT
    assert parsed.module_kwargs("go2connection")["puppy_enabled"] is True
    assert parsed.module_kwargs("persistentgo2planner")["navigation_speed_limit"] == 0.55
    baseline = BlueprintConfigParser(unitree_go2_agentic_persistent).parse([], environ={})
    assert baseline.module_kwargs("persistentgo2planner").get("navigation_speed_limit") is None
    assert parsed.module_kwargs("persistentgo2planner")["nearby_arrival_distance"] == 1.0
    assert parsed.module_kwargs("navigationskillcontainer")["vlm_url"] == settings.vlm_url
    assert parsed.module_kwargs("navigationskillcontainer")["vlm_model"] == settings.vlm_model


def test_legacy_console_settings_enable_gate_but_original_blueprint_is_unchanged(runtime):
    runtime.settings_path.write_text('{"robot_ip": "192.168.63.218"}')
    restored = ConsoleRuntime(runtime.project_dir, runtime.settings_path)
    assert restored.settings.auto_pause_fusion is True
    config = BlueprintConfigParser(unitree_go2_agentic_persistent).parse([], environ={})
    assert config.module_kwargs("persistentgo2map").get("auto_pause_fusion", False) is False


def test_demo_uses_new_explorer_but_original_blueprint_keeps_legacy():
    original = {atom.module for atom in unitree_go2_agentic_persistent.active_blueprints}
    demo = {atom.module for atom in unitree_go2_agentic_persistent_demo.active_blueprints}
    embedded = {atom.module for atom in unitree_go2_agentic_persistent_console.active_blueprints}
    assert WavefrontFrontierExplorer in original
    assert DemoExplorer not in original
    assert DemoExplorer in demo and WavefrontFrontierExplorer not in demo
    assert DemoExplorer in embedded and WavefrontFrontierExplorer not in embedded
    settings = ConsoleSettings()
    assert settings.argv()[4] == "unitree-go2-agentic-persistent-demo"
    parsed = BlueprintConfigParser(unitree_go2_agentic_persistent_demo).parse(
        settings.argv()[5:], environ={}
    )
    assert parsed.module_kwargs("go2connection")["puppy_enabled"] is True


@pytest.mark.parametrize("width", [0.29, 1.01, float("nan"), float("inf")])
def test_demo_planner_width_rejects_invalid_values(width):
    with pytest.raises(ValidationError):
        ConsoleSettings(planner_robot_width=width)


def test_demo_planner_width_persists_and_overrides_only_planner(runtime):
    runtime.save(SettingsUpdate(settings=ConsoleSettings(planner_robot_width=0.4)))
    restored = ConsoleRuntime(runtime.project_dir, runtime.settings_path)
    parsed = BlueprintConfigParser(unitree_go2_agentic_persistent_demo).parse(
        restored.settings.argv()[5:], environ={}
    )
    assert parsed.module_kwargs("persistentgo2planner")["robot_width"] == 0.4
    original = BlueprintConfigParser(unitree_go2_agentic_persistent).parse([], environ={})
    assert original.module_kwargs("persistentgo2planner").get("robot_width") is None
    module = PersistentGo2Planner(robot_width=0.4)
    try:
        assert module._planner._navigation_map._global_config.robot_width == 0.4
        assert module._planner._local_planner._global_config.robot_width == 0.4
        assert module.config.g.robot_width == 0.3
        assert module._planner._global_config.robot_rotation_diameter == 0.6
    finally:
        module._close_module()


@pytest.mark.parametrize("speed", [0.09, 0.56, float("nan"), float("inf")])
def test_demo_speed_default_rejects_invalid_limits(speed):
    with pytest.raises(ValidationError):
        ConsoleSettings(navigation_speed_limit=speed)


def test_saved_speed_default_reaches_demo_planner_after_restart(runtime):
    runtime.save(SettingsUpdate(settings=ConsoleSettings(navigation_speed_limit=0.25)))
    restored = ConsoleRuntime(runtime.project_dir, runtime.settings_path)
    parsed = BlueprintConfigParser(unitree_go2_agentic_persistent_demo).parse(
        restored.settings.argv()[5:], environ={}
    )
    assert parsed.module_kwargs("persistentgo2planner")["navigation_speed_limit"] == 0.25


@pytest.mark.parametrize(
    "values",
    [
        {"fusion_resume_speed": 0.01},
        {"fusion_stop_rotation_deg": 3},
        {"fusion_stationary_duration": float("nan")},
        {"fusion_sensor_timeout": 0.1},
    ],
)
def test_console_rejects_invalid_fusion_parameters(values):
    with pytest.raises(ValidationError):
        ConsoleSettings.model_validate(values)


def test_console_can_disable_auto_gate_and_pass_custom_thresholds():
    settings = ConsoleSettings(auto_pause_fusion=False, fusion_resume_speed=0.05)
    parsed = BlueprintConfigParser(unitree_go2_agentic_persistent).parse(
        settings.argv()[5:], environ={}
    )
    assert parsed.module_kwargs("persistentgo2map")["auto_pause_fusion"] is False
    assert parsed.module_kwargs("persistentgo2map")["fusion_resume_speed"] == 0.05


def test_embedded_console_preserves_rerun_blueprint_and_enables_web():
    original = next(
        atom
        for atom in unitree_go2_agentic_persistent.active_blueprints
        if atom.module is RerunBridgeModule
    )
    console = next(
        atom
        for atom in unitree_go2_agentic_persistent_console.active_blueprints
        if atom.module is RerunBridgeModule
    )
    assert console.kwargs == {**original.kwargs, "rerun_web": True}


def test_console_agent_uses_english_puppy_and_nearby_navigation_without_changing_original():
    original = next(
        atom
        for atom in unitree_go2_agentic_persistent.active_blueprints
        if atom.module is McpClient
    )
    console = next(
        atom
        for atom in unitree_go2_agentic_persistent_console.active_blueprints
        if atom.module is McpClient
    )
    prompt = console.kwargs["system_prompt"]
    assert prompt == CONSOLE_AGENT_PROMPT
    assert "English only" in prompt
    assert "My name is puppy, built from sedan" in prompt
    assert "navigate_near_memory_tag" in prompt
    assert "navigate_near_memory_tag" not in original.kwargs.get("system_prompt", "")


def test_console_puppy_is_opt_in_and_original_robot_default_is_off():
    console = next(
        atom
        for atom in unitree_go2_agentic_persistent_console.active_blueprints
        if atom.module is GO2Connection
    )
    assert console.kwargs["puppy_enabled"] is True
    assert ConnectionConfig().puppy_enabled is False


@pytest.fixture
def process(runtime, mocker):
    child = mocker.Mock()
    child.pid = 123456
    child.poll.return_value = None
    mocker.patch("dimos.web.console.settings.subprocess.Popen", return_value=child)
    mocker.patch("dimos.web.console.settings.threading.Thread")
    mocker.patch.object(runtime, "_ports_available")
    return child


def test_start_launches_owned_process_without_secret_arguments(runtime, process, mocker):
    (runtime.project_dir / ".env").write_text("OPENAI_API_KEY=test-only-key\n")
    runtime.save(SettingsUpdate(settings=ConsoleSettings(map_mode="new")))
    result = runtime.start()
    call = subprocess.Popen.call_args
    assert result["state"] == "starting"
    assert call.kwargs["env"]["OPENAI_API_KEY"] == "test-only-key"
    assert call.kwargs["env"]["OPENAI_BASE_URL"] == runtime.settings.agent_url
    assert "test-only-key" not in " ".join(call.args[0])
    assert call.kwargs["cwd"] == runtime.project_dir
    assert "shell" not in call.kwargs
    assert runtime._redact("key=test-only-key") == "key=[REDACTED]"


def test_start_rejects_duplicate_and_settings_locked_while_alive(runtime, process):
    runtime.settings = ConsoleSettings(map_mode="new")
    runtime.start()
    with pytest.raises(ValueError, match="already owns"):
        runtime.start()
    with pytest.raises(ValueError, match="Stop the stack"):
        runtime.save(SettingsUpdate(settings=ConsoleSettings()))


def test_new_map_never_reuses_old_semantic_directory(runtime, process):
    runtime.settings = ConsoleSettings(map_mode="new", scene_map_dir="old_scene")
    scene = runtime.project_dir / "old_scene"
    scene.mkdir()
    (scene / "tags.json").write_text("{}")
    with pytest.raises(ValueError, match="overwrite confirmation"):
        runtime.start()
    assert (scene / "tags.json").read_text() == "{}"


def test_confirmed_new_map_backs_up_scene_and_launches_with_original_path(runtime, process):
    runtime.settings = ConsoleSettings(map_mode="new", scene_map_dir="old_scene")
    scene = runtime.project_dir / "old_scene"
    scene.mkdir()
    (scene / "tags.json").write_text("old tags")
    plan = runtime.prepare_start()
    assert plan["overwrite_required"] is True
    assert (scene / "tags.json").read_text() == "old tags"
    assert runtime.start(plan["overwrite_token"])["state"] == "starting"
    backups = list(runtime.project_dir.glob("old_scene.backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "tags.json").read_text() == "old tags"
    assert not scene.exists()


def test_failed_launch_restores_confirmed_scene(runtime, process):
    runtime.settings = ConsoleSettings(map_mode="new", scene_map_dir="old_scene")
    scene = runtime.project_dir / "old_scene"
    scene.mkdir()
    (scene / "tags.json").write_text("old tags")
    plan = runtime.prepare_start()
    subprocess.Popen.side_effect = OSError("launch failed")
    with pytest.raises(OSError, match="launch failed"):
        runtime.start(plan["overwrite_token"])
    assert (scene / "tags.json").read_text() == "old tags"


def test_changed_settings_invalidate_overwrite_confirmation(runtime, process):
    runtime.settings = ConsoleSettings(map_mode="new", scene_map_dir="old_scene")
    scene = runtime.project_dir / "old_scene"
    scene.mkdir()
    (scene / "tags.json").write_text("old tags")
    plan = runtime.prepare_start()
    runtime.settings = runtime.settings.model_copy(update={"robot_ip": "192.168.1.2"})
    with pytest.raises(ValueError, match="overwrite confirmation"):
        runtime.start(plan["overwrite_token"])
    assert (scene / "tags.json").read_text() == "old tags"


def test_restore_requires_saved_map(runtime, process):
    with pytest.raises(ValueError, match="Saved map does not exist"):
        runtime.start()


@pytest.mark.parametrize("capture", ["manual", "rotation"])
def test_declining_overwrite_preserves_scene_and_starts_restore_alignment(
    runtime, process, capture
):
    runtime.settings = ConsoleSettings(
        map_mode="new", scene_map_dir="old_scene", capture_mode=capture
    )
    scene = runtime.project_dir / "old_scene"
    scene.mkdir()
    (scene / "map.pc2.lcm").write_bytes(b"saved map")
    (scene / "tags.json").write_text("old tags")
    assert runtime.prepare_start()["restore_available"] is True
    assert runtime.start(use_existing_map=True)["state"] == "starting"
    argv = subprocess.Popen.call_args.args[0]
    parsed = BlueprintConfigParser(unitree_go2_agentic_persistent).parse(argv[5:], environ={})
    config = parsed.module_kwargs("persistentgo2map")
    assert config["create_new"] is False
    assert config["manual_capture"] is (capture == "manual")
    assert config["startup_rotation"] is (capture == "rotation")
    assert (scene / "map.pc2.lcm").read_bytes() == b"saved map"
    assert (scene / "tags.json").read_text() == "old tags"
    assert list(runtime.project_dir.glob("old_scene.backup-*")) == []
    assert runtime.settings.map_mode == "new"


def test_declining_overwrite_without_saved_map_refuses_launch(runtime, process):
    runtime.settings = ConsoleSettings(map_mode="new", scene_map_dir="old_scene")
    scene = runtime.project_dir / "old_scene"
    scene.mkdir()
    (scene / "tags.json").write_text("old tags")
    assert runtime.prepare_start()["restore_available"] is False
    with pytest.raises(ValueError, match="Saved map does not exist"):
        runtime.start(use_existing_map=True)
    subprocess.Popen.assert_not_called()
    assert (scene / "tags.json").read_text() == "old tags"


def test_stop_only_signals_owned_child_and_waits_for_graceful_exit(runtime, process):
    runtime._process = process
    process.poll.side_effect = [None, 0]
    assert runtime.stop()["state"] == "stopped"
    process.send_signal.assert_called_once_with(signal.SIGTERM)
    process.wait.assert_called_once_with(timeout=30)


def test_stop_timeout_does_not_force_kill(runtime, process):
    runtime._process = process
    process.wait.side_effect = subprocess.TimeoutExpired("dimos", 30)
    with pytest.raises(ValueError, match="No forced kill"):
        runtime.stop()
    process.kill.assert_not_called()


async def test_settings_api_masks_secrets_and_requires_token(runtime):
    (runtime.project_dir / ".env").write_text("OPENAI_API_KEY=test-only-key\n")
    module = RobotConsoleModule()
    module.runtime = runtime
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=module._build_app()), base_url="http://test"
        ) as client:
            token = (await client.get("/api/config")).json()["csrf_token"]
            payload = {
                "settings": ConsoleSettings().model_dump(),
            }
            assert (await client.post("/api/settings", json=payload)).status_code == 403
            response = await client.post(
                "/api/settings", json=payload, headers={"X-Console-Token": token}
            )
            assert response.json()["ok"] is True
            assert "test-only-key" not in response.text
            assert response.json()["secrets"]["OPENAI_API_KEY"] is True
            rejected = await client.post(
                "/api/settings",
                json={**payload, "openai_api_key": "replacement"},
                headers={"X-Console-Token": token},
            )
            assert rejected.json()["ok"] is False
            assert (runtime.project_dir / ".env").read_text() == "OPENAI_API_KEY=test-only-key\n"
            csrf = await client.post(
                "/api/stack/start",
                json={"confirmed": True},
                headers={"X-Console-Token": token, "Sec-Fetch-Site": "cross-site"},
            )
            assert csrf.status_code == 403
            confirm = await client.post(
                "/api/stack/start", json={}, headers={"X-Console-Token": token}
            )
            assert confirm.json()["error"] == "Human confirmation is required"
            chat = await client.post(
                "/api/chat", json={"message": "hi"}, headers={"X-Console-Token": token}
            )
            assert chat.json()["error"] == "Robot stack is not ready"
    finally:
        module._close_module()


@pytest.mark.parametrize("port", [8090, RERUN_GRPC_PORT])
def test_port_collision_does_not_save_invalid_settings(runtime, port):
    with pytest.raises(ValueError, match="different ports"):
        runtime.save(SettingsUpdate(settings=ConsoleSettings(mcp_port=port)))
    assert not runtime.settings_path.exists()


def test_invalid_registry_blocks_start_with_actionable_error(runtime, tmp_path, monkeypatch):
    monkeypatch.setattr("dimos.web.console.settings.REGISTRY_DIR", tmp_path)
    (tmp_path / "broken.json").write_text("not json")
    with pytest.raises(ValueError, match="invalid run registry broken.json"):
        runtime.start()


def test_port_probe_uses_saved_rerun_data_port(runtime, mocker, tmp_path, monkeypatch):
    monkeypatch.setattr("dimos.web.console.settings.REGISTRY_DIR", tmp_path / "runs")
    runtime.save(SettingsUpdate(settings=ConsoleSettings(rerun_grpc_port=9887)))
    restored = ConsoleRuntime(runtime.project_dir, runtime.settings_path)
    probe = mocker.patch("dimos.web.console.settings.socket.socket")
    restored._ports_available()
    assert probe.return_value.__enter__.return_value.bind.call_args_list == [
        mocker.call(("127.0.0.1", restored.settings.mcp_port)),
        mocker.call(("127.0.0.1", restored.settings.rerun_web_port)),
        mocker.call(("127.0.0.1", 9887)),
    ]


async def test_shutdown_closes_console_even_when_stack_cleanup_fails(runtime, mocker):
    mocker.patch("sys.argv", ["console"])
    mocker.patch.object(entrypoint, "ConsoleRuntime", return_value=runtime)
    module = mocker.Mock()
    app = FastAPI()
    module._build_app.return_value = app
    mocker.patch.object(entrypoint, "RobotConsoleModule", return_value=module)
    mocker.patch.object(entrypoint.uvicorn, "run")
    entrypoint.main()
    mocker.patch.object(runtime, "shutdown", side_effect=RuntimeError("Worker did not stop"))

    with pytest.raises(RuntimeError, match="Worker did not stop"):
        async with app.router.lifespan_context(app):
            assert module._console_loop is not None

    module._teardown_agent_streams.assert_called_once_with()
    module._close_module.assert_called_once_with()
    assert module._console_loop is None


def test_exit_shutdown_escalates_and_cleans_owned_workers(runtime, process, mocker):
    runtime._process = process
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("dimos", 5), 0]
    child = mocker.Mock()
    parent = mocker.patch("dimos.web.console.settings.psutil.Process").return_value
    parent.children.return_value = [child]
    wait = mocker.patch(
        "dimos.web.console.settings.psutil.wait_procs",
        side_effect=[([], [child]), ([], [child]), ([child], [])],
    )

    runtime.shutdown()
    runtime.shutdown()

    process.send_signal.assert_called_once_with(signal.SIGTERM)
    process.kill.assert_called_once_with()
    child.terminate.assert_called_once_with()
    child.kill.assert_called_once_with()
    assert wait.call_count == 3
    assert runtime.status()["state"] == "stopped"


def test_exit_shutdown_does_not_force_kill_a_clean_stack(runtime, process, mocker):
    runtime._process = process
    process.poll.return_value = None
    mocker.patch(
        "dimos.web.console.settings.psutil.Process"
    ).return_value.children.return_value = []
    mocker.patch("dimos.web.console.settings.psutil.wait_procs", return_value=([], []))

    runtime.shutdown()

    process.send_signal.assert_called_once_with(signal.SIGTERM)
    process.kill.assert_not_called()


def test_terminal_close_uses_uvicorn_graceful_sigterm(mocker):
    raised = mocker.patch.object(entrypoint.signal, "raise_signal")
    entrypoint._on_terminal_close(signal.SIGHUP, None)
    raised.assert_called_once_with(signal.SIGTERM)


@pytest.fixture
def owned_process_tree(runtime):
    worker_code = (
        "import signal; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready', flush=True); signal.pause()"
    )
    parent_code = (
        "import subprocess, sys, signal; "
        f"worker = subprocess.Popen([sys.executable, '-c', {worker_code!r}], "
        "stdout=subprocess.PIPE, text=True); "
        "worker.stdout.readline(); print(worker.pid, flush=True); signal.pause()"
    )
    with subprocess.Popen(
        [sys.executable, "-c", parent_code], stdout=subprocess.PIPE, text=True
    ) as process:
        worker = None
        try:
            assert process.stdout is not None
            worker = psutil.Process(int(process.stdout.readline()))
            runtime._process = process
            yield process, worker
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if worker is not None:
                try:
                    worker.kill()
                    worker.wait(timeout=5)
                except psutil.NoSuchProcess:
                    pass


@pytest.mark.timeout(20)
def test_exit_shutdown_removes_real_owned_process_tree(runtime, owned_process_tree):
    process, worker = owned_process_tree

    runtime.shutdown()

    assert process.poll() is not None
    assert not worker.is_running() or worker.status() == psutil.STATUS_ZOMBIE
    assert runtime.status()["state"] == "stopped"
