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

"""Local settings and ownership-scoped robot process management."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
import ipaddress
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from typing import Any, Literal, TextIO
from urllib.parse import urlsplit
import uuid

from dotenv import dotenv_values
from fastapi import FastAPI
import psutil
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
import requests

from dimos.constants import CONFIG_DIR
from dimos.core.global_config import global_config
from dimos.core.run_registry import REGISTRY_DIR, RunEntry, is_pid_alive
from dimos.mapping.relocalization.go2.fusion_gate import FusionGateConfig
from dimos.utils.logging_config import setup_logger
from dimos.visualization.rerun.constants import RERUN_GRPC_PORT, RERUN_WEB_VIEWER_PORT
from dimos.web.console.profiles import (
    DEFAULT_PROFILE,
    PROFILES,
    Connection,
    StackProfile,
)
from dimos.web.console.prompts import CONSOLE_AGENT_PROMPT, CONSOLE_PEOPLE_AGENT_PROMPT

logger = setup_logger()

# Logged by ModuleCoordinator.build once every module of a blueprint has started.
STARTED_MARKER = "Blueprint started"
# Simulated persistent-map sessions each get a fresh scene folder; keep the newest few.
SIM_SCENES_KEPT = 5
_SIM_SCENE_NAME = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")


class ConsoleSettings(FusionGateConfig):
    model_config = ConfigDict(extra="forbid")

    auto_pause_fusion: bool = True
    profile: str = DEFAULT_PROFILE
    robot_ip: str = "192.168.123.161"
    replay: bool = False
    # Run the MuJoCo simulator instead of a robot (never together with replay).
    simulation: bool = False
    replay_db: str = "go2_short"
    map_mode: Literal["new", "restore"] = "restore"
    scene_map_dir: str = "assets/scene_maps/sedan_office_persistent"
    capture_mode: Literal["manual", "rotation"] = "manual"
    rotation_speed: float = Field(default=0.15, gt=0, le=0.3)
    rotation_duration: float = Field(default=20, gt=0, le=60)
    obstacle_avoidance: bool = True
    agent_url: str = "https://api.openai.com/v1"
    agent_model: str = "gpt-5.6-luna"
    puppy_noise_reduction: bool = True
    vlm_url: str = "https://api.openai.com"
    vlm_model: str = "gpt-5.6-luna"
    place_tagging: bool = True
    object_tagging: bool = True
    vlm_distance_m: float = Field(default=1, gt=0, le=100)
    object_segmenter: Literal["auto", "yolo", "vlm"] = "yolo"
    pgo_enabled: bool = False
    # Person following (stacks that have the person skills). The head camera only sees a
    # person's top from a few metres away, and the agent tells people apart by what they
    # wear, so the default stays well back.
    follow_distance_m: float = Field(default=3.0, ge=0.5, le=8.0, allow_inf_nan=False)
    follow_update_threshold_m: float = Field(default=0.5, gt=0.0, le=3.0, allow_inf_nan=False)
    search_step_deg: float = Field(default=60.0, ge=0.0, le=120.0, allow_inf_nan=False)
    nearby_arrival_distance: float = Field(default=1, ge=0.3, le=3, allow_inf_nan=False)
    planner_robot_width: float = Field(default=0.3, ge=0.05, le=1.0, allow_inf_nan=False)
    navigation_speed_limit: float = Field(default=0.55, ge=0.1, le=0.55, allow_inf_nan=False)
    mcp_port: int = Field(default=global_config.mcp_port, ge=1024, le=65535)
    rerun_web_port: int = Field(default=RERUN_WEB_VIEWER_PORT, ge=1024, le=65535)
    rerun_grpc_port: int = Field(default=RERUN_GRPC_PORT, ge=1024, le=65535)

    @field_validator("profile")
    @classmethod
    def known_profile(cls, value: str) -> str:
        if value not in PROFILES:
            raise ValueError(f"Unknown stack: {value!r}; choose one of {sorted(PROFILES)}")
        return value

    @model_validator(mode="after")
    def valid_connection(self) -> ConsoleSettings:
        if self.replay and self.simulation:
            raise ValueError("Replay and simulation cannot both be on")
        profile = PROFILES[self.profile]
        if self.connection not in profile.connections:
            raise ValueError(f"{profile.label} cannot run with the {self.connection} connection")
        return self

    @property
    def connection(self) -> Connection:
        return "simulation" if self.simulation else "replay" if self.replay else "robot"

    @property
    def stack(self) -> StackProfile:
        return PROFILES[self.profile]

    @field_validator("robot_ip")
    @classmethod
    def valid_ip(cls, value: str) -> str:
        ipaddress.ip_address(value)
        return value

    @field_validator("agent_url", "vlm_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Use an HTTP(S) URL without embedded credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("Base URLs cannot contain query strings or fragments")
        return value.rstrip("/")

    @field_validator("agent_model", "vlm_model", "scene_map_dir", "replay_db")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("Value must be nonempty without control characters")
        return value.strip()

    @field_validator("rotation_speed", "rotation_duration", "vlm_distance_m")
    @classmethod
    def finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Value must be finite")
        return value

    def argv(self) -> list[str]:
        profile = self.stack
        args = [
            sys.executable,
            "-m",
            "dimos.cli.dimos",
            "run",
            profile.blueprint_for(self.connection),
            *self._connection_args(),
            *self._rerun_args(),
        ]
        if profile.has_agent:
            args += self._agent_args()
        if profile.has_map:
            args += self._persistent_args()
        if profile.has_people:
            args += self._people_args()
        return args

    def _connection_args(self) -> list[str]:
        return [
            f"--robot-ip={self.robot_ip}",
            f"--replay={'true' if self.replay else 'false'}",
            f"--replay-db={self.replay_db}",
            f"--obstacle-avoidance={'true' if self.obstacle_avoidance else 'false'}",
            *(["--simulation=mujoco"] if self.simulation else []),
        ]

    def _rerun_args(self) -> list[str]:
        return [
            "--rerunbridgemodule.rerun-web=true",
            f"--rerunbridgemodule.web-port={self.rerun_web_port}",
            f"--rerunbridgemodule.connect-url=rerun+http://127.0.0.1:{self.rerun_grpc_port}/proxy",
        ]

    def _agent_args(self) -> list[str]:
        args = [
            f"--mcp-port={self.mcp_port}",
            f"--mcpclient.mcp-server-url=http://127.0.0.1:{self.mcp_port}/mcp",
            f"--mcpclient.model={self.agent_model}",
        ]
        if self.stack.has_map:
            # The persistent stacks speak as the console's persona; the demo keeps its own.
            prompt = CONSOLE_PEOPLE_AGENT_PROMPT if self.stack.has_people else CONSOLE_AGENT_PROMPT
            args.append(f"--mcpclient.system-prompt={prompt}")
        return args

    def _people_args(self) -> list[str]:
        args = [
            f"--personnavigationskillcontainer.vlm-url={self.vlm_url}",
            f"--personnavigationskillcontainer.vlm-model={self.vlm_model}",
            f"--personnavigationskillcontainer.search-step-deg={self.search_step_deg}",
            f"--goaltracker.follow-distance-m={self.follow_distance_m}",
            f"--goaltracker.update-threshold-m={self.follow_update_threshold_m}",
        ]
        if not self.stack.has_map:
            # The persistent flags already point this container at the same service.
            args += [
                f"--navigationskillcontainer.vlm-url={self.vlm_url}",
                f"--navigationskillcontainer.vlm-model={self.vlm_model}",
            ]
        return args

    def _persistent_args(self) -> list[str]:
        scene = Path(self.scene_map_dir)
        args = [
            "--go2connection.puppy-enabled=true",
            f"--go2connection.puppy-noise-reduction={str(self.puppy_noise_reduction).lower()}",
            f"--persistentgo2planner.nearby-arrival-distance={self.nearby_arrival_distance}",
            f"--persistentgo2planner.robot-width={self.planner_robot_width}",
            f"--persistentgo2planner.navigation-speed-limit={self.navigation_speed_limit}",
            f"--persistentgo2map.map-file={scene / 'map.pc2.lcm'}",
            f"--persistentgo2map.create-new={'true' if self.map_mode == 'new' else 'false'}",
            f"--persistentgo2map.manual-capture={'true' if self.map_mode == 'restore' and self.capture_mode == 'manual' else 'false'}",
            f"--persistentgo2map.startup-rotation={'true' if self.map_mode == 'restore' and self.capture_mode == 'rotation' else 'false'}",
            f"--persistentgo2map.rotation-speed={self.rotation_speed}",
            f"--persistentgo2map.rotation-duration={self.rotation_duration}",
            f"--spatialmemory.scene-map-dir={self.scene_map_dir}",
            f"--spatialmemory.vlm-url={self.vlm_url}",
            f"--spatialmemory.vlm-model={self.vlm_model}",
            f"--navigationskillcontainer.vlm-url={self.vlm_url}",
            f"--navigationskillcontainer.vlm-model={self.vlm_model}",
            f"--go2connection.tts-url={self.vlm_url.rstrip('/') if self.vlm_url.rstrip('/').endswith('/v1') else self.vlm_url.rstrip('/') + '/v1'}",
            f"--spatialmemory.vlm-enable-place-tagging={'true' if self.place_tagging else 'false'}",
            f"--spatialmemory.vlm-enable-object-tagging={'true' if self.object_tagging else 'false'}",
            f"--spatialmemory.vlm-distance-m={self.vlm_distance_m}",
            f"--spatialmemory.object-segmenter={self.object_segmenter}",
            f"--persistentgo2map.pgo-enabled={'true' if self.pgo_enabled else 'false'}",
        ]
        for field in FusionGateConfig.model_fields:
            value = getattr(self, field)
            encoded = str(value).lower() if isinstance(value, bool) else str(value)
            args.append(f"--persistentgo2map.{field.replace('_', '-')}={encoded}")
        return args


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settings: ConsoleSettings


class ConsoleRuntime:
    def __init__(
        self,
        project_dir: Path,
        settings_path: Path | None = None,
        console_port: int = 8090,
        log_dir: Path | None = None,
    ) -> None:
        self.project_dir = project_dir.resolve()
        self.settings_path = settings_path or CONFIG_DIR / "robot-console.json"
        self.console_port = console_port
        self.settings = (
            ConsoleSettings.model_validate_json(self.settings_path.read_text())
            if self.settings_path.exists()
            else ConsoleSettings()
        )
        self._validate_ports(self.settings)
        self._secrets: dict[str, str] = {}
        self._process: subprocess.Popen[str] | None = None
        self._monitor: threading.Thread | None = None
        self._lock = threading.RLock()
        self._halt = threading.Event()
        self._lines: deque[str] = deque(maxlen=200)
        self._state = "stopped"
        self._error: str | None = None
        self._overwrite_token: str | None = None
        self._overwrite_settings: ConsoleSettings | None = None
        self._launch_profile: StackProfile | None = None
        self._started_seen = False
        self._log_dir: Path | None = log_dir
        self._stack_log: TextIO | None = None
        if log_dir is not None:
            self._open_stack_log()
        self.on_event: Callable[[dict[str, Any]], None] = lambda event: None
        self.on_settings: Callable[[ConsoleSettings], None] = lambda settings: None

    @property
    def log_dir(self) -> Path | None:
        """Directory where this console's structured and stack logs live."""
        return self._log_dir

    def _open_stack_log(self) -> None:
        """Open the captured robot-stack log file, degrading gracefully."""
        if self._log_dir is None:
            return
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            self._stack_log = (self._log_dir / "stack.log").open("a", encoding="utf-8", buffering=1)
        except OSError:
            logger.warning("Could not open console stack log", log_dir=str(self._log_dir))
            self._stack_log = None

    def _append_stack_log(self, line: str) -> None:
        """Persist a captured stack line so it survives the in-memory ring."""
        if self._stack_log is None:
            return
        try:
            self._stack_log.write(line + "\n")
        except OSError:
            pass

    def _close_stack_log(self) -> None:
        if self._stack_log is not None:
            try:
                self._stack_log.close()
            except OSError:
                pass
            self._stack_log = None

    @property
    def profile(self) -> StackProfile:
        """The running stack's profile, or the selected one while stopped."""
        with self._lock:
            running = self._process is not None and self._process.poll() is None
            if running and self._launch_profile is not None:
                return self._launch_profile
            return self.settings.stack

    @property
    def sim_scene_root(self) -> Path:
        return self.settings_path.parent / "sim-scenes"

    @property
    def ready(self) -> bool:
        return bool(self.status()["state"] == "running")

    def status(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            code = process.poll() if process is not None else None
            if code is not None and self._state not in {"stopped", "failed"}:
                self._state = "stopped" if code == 0 else "failed"
                self._error = (
                    None if code == 0 else f"Stack exited with code {code}; see Stack logs"
                )
            return {
                "state": self._state,
                "pid": process.pid if process is not None and code is None else None,
                "error": self._error,
            }

    def public_settings(self) -> dict[str, Any]:
        with self._lock:
            secrets = self._read_env_keys()
            return {
                "settings": self.settings.model_dump(),
                "secrets": {
                    name: bool(secrets.get(name))
                    for name in ("OPENAI_API_KEY", "UNITREE_AES_128_KEY")
                },
                "command": self.settings.argv()[3:],
                "profiles": [profile.to_dict() for profile in PROFILES.values()],
            }

    def _read_env_keys(self) -> dict[str, str]:
        path = self.project_dir / ".env"
        if not path.exists():
            return {}
        with path.open() as file:
            values = dotenv_values(stream=file, interpolate=False)
        return {
            name: value.strip()
            for name in ("OPENAI_API_KEY", "UNITREE_AES_128_KEY")
            if (value := values.get(name)) and value.strip()
        }

    def _validate_ports(self, settings: ConsoleSettings) -> None:
        if (
            len(
                {
                    self.console_port,
                    settings.mcp_port,
                    settings.rerun_web_port,
                    settings.rerun_grpc_port,
                }
            )
            != 4
        ):
            raise ValueError("Console, MCP, Rerun Web and Rerun gRPC must use different ports")

    def save(self, update: SettingsUpdate) -> dict[str, Any]:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise ValueError("Stop the stack before changing settings")
            self._validate_ports(update.settings)
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            fd, filename = tempfile.mkstemp(dir=self.settings_path.parent)
            try:
                with os.fdopen(fd, "w") as file:
                    file.write(update.settings.model_dump_json(indent=2))
                os.replace(filename, self.settings_path)
            finally:
                Path(filename).unlink(missing_ok=True)
            self.settings = update.settings
            self.on_settings(self.settings)
            return self.public_settings()

    def _redact(self, text: str) -> str:
        for name in ("OPENAI_API_KEY", "UNITREE_AES_128_KEY"):
            for value in (self._secrets.get(name), os.environ.get(name)):
                if value:
                    text = text.replace(value, "[REDACTED]")
        return text

    def _ports_available(self) -> None:
        for path in REGISTRY_DIR.glob("*.json"):
            try:
                entry = RunEntry.load(path)
                alive = is_pid_alive(entry.pid)
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError(
                    f"Cannot safely check other stacks: invalid run registry {path.name}"
                ) from exc
            if alive:
                raise ValueError(
                    "Another DimOS stack is running. Stop it in its own terminal before starting this console's stack."
                )
        ports = [self.settings.rerun_web_port, self.settings.rerun_grpc_port]
        if self.settings.stack.has_agent:
            ports.insert(0, self.settings.mcp_port)
        for port in ports:
            with socket.socket() as probe:
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError as exc:
                    raise ValueError(
                        f"Port {port} is occupied; stop the existing stack or change Settings"
                    ) from exc

    def _new_sim_scene(self) -> Path:
        """A fresh, empty scene folder for one simulated session.

        Simulated maps are throwaway, so each start gets its own folder (no overwrite
        prompt, and the real scene is never touched) and only the newest few are kept.
        """
        root = self.sim_scene_root
        root.mkdir(parents=True, exist_ok=True)
        scene = root / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        scene.mkdir()
        old = sorted(
            path for path in root.iterdir() if path.is_dir() and _SIM_SCENE_NAME.match(path.name)
        )
        for path in old[: max(0, len(old) - SIM_SCENES_KEPT)]:
            shutil.rmtree(path, ignore_errors=True)
        return scene

    def _scene_path(self) -> Path:
        scene = Path(self.settings.scene_map_dir)
        if not scene.is_absolute():
            scene = self.project_dir / scene
        scene = scene.resolve()
        for protected in (self.project_dir, Path.home().resolve()):
            if scene == protected or scene in protected.parents:
                raise ValueError(
                    "Scene directory cannot be the project, home or their parent directories"
                )
        return scene

    def prepare_start(self) -> dict[str, Any]:
        with self._lock:
            if not self.settings.stack.has_map or self.settings.simulation:
                # Nothing to overwrite: no map, or a fresh simulated scene is made at start.
                self._overwrite_token = self._overwrite_settings = None
                return {
                    "ok": True,
                    "confirm_new_map": False,
                    "overwrite_required": False,
                    "scene_directory": None,
                    "overwrite_token": None,
                    "restore_available": False,
                }
            scene = self._scene_path()
            overwrite = self.settings.map_mode == "new" and scene.exists() and any(scene.iterdir())
            self._overwrite_token = uuid.uuid4().hex if overwrite else None
            self._overwrite_settings = self.settings.model_copy(deep=True) if overwrite else None
            return {
                "ok": True,
                "confirm_new_map": self.settings.map_mode == "new",
                "overwrite_required": overwrite,
                "scene_directory": str(scene),
                "overwrite_token": self._overwrite_token,
                "restore_available": (scene / "map.pc2.lcm").is_file(),
            }

    def start(
        self, overwrite_token: str | None = None, *, use_existing_map: bool = False
    ) -> dict[str, Any]:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise ValueError("This console already owns a running stack")
            self._ports_available()
            if not self.project_dir.is_dir():
                raise ValueError("Project directory does not exist")
            profile = self.settings.stack
            launch_settings = self.settings
            scene: Path | None = None
            map_path: Path | None = None
            overwrite = False
            if profile.has_map and self.settings.simulation:
                # A simulated session always builds a new map in its own fresh folder.
                scene = self._new_sim_scene()
                launch_settings = self.settings.model_copy(
                    update={"map_mode": "new", "scene_map_dir": str(scene)}
                )
            elif profile.has_map:
                scene = self._scene_path()
                map_path = scene / "map.pc2.lcm"
                launch_settings = (
                    self.settings.model_copy(update={"map_mode": "restore"})
                    if use_existing_map
                    else self.settings
                )
                overwrite = (
                    launch_settings.map_mode == "new" and scene.exists() and any(scene.iterdir())
                )
                if overwrite and (
                    not overwrite_token
                    or overwrite_token != self._overwrite_token
                    or self.settings != self._overwrite_settings
                ):
                    raise ValueError(
                        "Existing scene requires overwrite confirmation. Click Start again."
                    )
                if launch_settings.map_mode == "restore" and not map_path.is_file():
                    raise ValueError("Saved map does not exist; select New map for the first run")
            env = os.environ.copy()
            self._secrets = self._read_env_keys()
            for name in ("OPENAI_API_KEY", "UNITREE_AES_128_KEY"):
                env.pop(name, None)
            env.update(self._secrets)
            env["OPENAI_BASE_URL"] = self.settings.agent_url
            # Keep the independent console and child on the same local transport.
            env["DIMOS_TRANSPORT"] = global_config.transport
            env["DIMOS_LISTEN_HOST"] = "127.0.0.1"
            self._halt.clear()
            self._lines.clear()
            backup = None
            if overwrite:
                assert scene is not None
                backup = scene.with_name(f"{scene.name}.backup-{uuid.uuid4().hex}")
                scene.rename(backup)
                self._overwrite_token = None
                self._overwrite_settings = None
            self._launch_profile = profile
            self._started_seen = False
            try:
                self._process = subprocess.Popen(
                    launch_settings.argv(),
                    cwd=self.project_dir,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            except OSError:
                if backup is not None:
                    assert scene is not None
                    backup.rename(scene)
                raise
            if backup is not None:
                message = f"Previous scene backed up to {backup}; creating a new map at {scene}"
                self._lines.append(message)
                self.on_event({"type": "stack_log", "text": message})
            if use_existing_map:
                self._overwrite_token = None
                self._overwrite_settings = None
                message = f"Using existing map at {map_path}; Restore alignment is required."
                self._lines.append(message)
                self.on_event({"type": "stack_log", "text": message})
            if profile.has_map and self.settings.simulation:
                message = f"Simulated session: building a new map in {scene}"
                self._lines.append(message)
                self.on_event({"type": "stack_log", "text": message})
            self._state, self._error = "starting", None
            logger.info(
                "Console started robot stack",
                pid=self._process.pid,
                profile=profile.key,
                connection=self.settings.connection,
                map_mode=launch_settings.map_mode if profile.has_map else None,
                scene=str(scene) if scene is not None else None,
                log_dir=str(self._log_dir),
            )
            self._monitor = threading.Thread(
                target=self._watch, name="console-stack-monitor", daemon=True
            )
            self._monitor.start()
            return self.status()

    def _watch(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        reader = threading.Thread(
            target=self._read_output, args=(process,), name="console-stack-output", daemon=True
        )
        reader.start()
        try:
            while process.poll() is None and not self._halt.wait(0.5):
                if self._state == "starting":
                    profile = self._launch_profile
                    if profile is not None and not profile.ready_tools:
                        if self._started_seen:
                            with self._lock:
                                if self._state == "starting":
                                    self._state = "running"
                        continue
                    required = profile.ready_tools if profile is not None else frozenset()
                    try:
                        response = requests.post(
                            f"http://127.0.0.1:{self.settings.mcp_port}/mcp",
                            json={"jsonrpc": "2.0", "id": "console-ready", "method": "tools/list"},
                            timeout=1,
                        )
                        response.raise_for_status()
                        data = response.json()
                        if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
                            raise ValueError("MCP tool list has no result")
                        tools = data["result"].get("tools")
                        if not isinstance(tools, list):
                            raise ValueError("MCP tool list is invalid")
                        names = {tool["name"] for tool in tools if isinstance(tool, dict)}
                        if required <= names:
                            with self._lock:
                                if self._state == "starting":
                                    self._state = "running"
                    except (requests.RequestException, ValueError, KeyError, TypeError):
                        continue
            process.wait()
            reader.join(timeout=5)
            self.on_event({"type": "stack", **self.status()})
        finally:
            process.stdout.close()

    def _read_output(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            safe = self._redact(line.rstrip())
            if STARTED_MARKER in safe:
                self._started_seen = True
            with self._lock:
                self._lines.append(safe)
                self._append_stack_log(safe)
            self.on_event({"type": "stack_log", "text": safe})

    def logs(self) -> list[str]:
        with self._lock:
            return list(self._lines)

    def stop(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                return self.status()

            self._state = "stopping"
            logger.info("Stopping robot stack", pid=process.pid)
            try:
                process.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass  # The owned child exited between poll and signal.
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired as exc:
            logger.error(
                "Graceful stop timed out; no forced kill was issued. Check the stack logs.",
                pid=process.pid,
            )
            raise ValueError(
                "Graceful stop timed out; check the stack terminal. No forced kill was issued."
            ) from exc
        self._halt.set()
        if self._monitor is not None:
            self._monitor.join(timeout=5)
        with self._lock:
            self._state = "stopped"
            self._error = None
        return self.status()

    def shutdown(self) -> None:
        """Stop this console's stack and descendants, escalating only on exit."""
        with self._lock:
            process = self._process
            if process is None:
                return
            try:
                children = psutil.Process(process.pid).children(recursive=True)
            except psutil.NoSuchProcess:
                children = []
            logger.info("Console exiting: stopping its robot stack", pid=process.pid)
            if process.poll() is None:
                try:
                    process.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("Console exit: stack did not stop in 5s; sending SIGKILL")
            process.kill()
            process.wait(timeout=5)

        # Capture descendants before stopping the parent: workers may be reparented.
        _, alive = psutil.wait_procs(children, timeout=2)
        for child in alive:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(alive, timeout=2)
        for child in alive:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(alive, timeout=2)
        if alive:
            raise RuntimeError(
                f"Console exit: could not stop worker PIDs {[child.pid for child in alive]}"
            )
        self._halt.set()
        if self._monitor is not None:
            self._monitor.join(timeout=5)
            if self._monitor.is_alive():
                raise RuntimeError("Console exit: stack monitor did not stop")
        if process.stdout is not None:
            process.stdout.close()
        with self._lock:
            self._process = None
            self._state, self._error = "stopped", None
        self._close_stack_log()
        logger.info("Console robot stack stopped")


def register_runtime_routes(
    app: FastAPI,
    runtime: ConsoleRuntime,
    prepare_shutdown: Callable[[bool], Awaitable[dict[str, Any]]] | None = None,
) -> None:
    lifecycle_lock = asyncio.Lock()
    shutdown_result: dict[str, Any] | None = None

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return runtime.public_settings()

    @app.post("/api/settings")
    async def save_settings(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            update = SettingsUpdate.model_validate(payload)
            result = await asyncio.to_thread(runtime.save, update)
        except ValidationError as exc:
            errors = "; ".join(
                f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                for e in exc.errors(include_input=False)
            )
            return {"ok": False, "error": errors}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except OSError:
            return {
                "ok": False,
                "error": "Could not save settings; check the settings file permissions",
            }
        return {"ok": True, **result}

    @app.get("/api/stack")
    def stack_status() -> dict[str, Any]:
        return runtime.status()

    @app.get("/api/stack/logs")
    def stack_logs() -> dict[str, Any]:
        return {"lines": runtime.logs()}

    @app.get("/api/log")
    def log_info() -> dict[str, Any]:
        return {
            "log_dir": str(runtime.log_dir) if runtime.log_dir else None,
            "debug": os.environ.get("DIMOS_LOG_LEVEL", "INFO") == "DEBUG",
        }

    @app.post("/api/stack/prepare")
    def prepare_start() -> dict[str, Any]:
        try:
            return runtime.prepare_start()
        except (ValueError, OSError) as exc:
            return {"ok": False, "error": str(exc)}

    @app.post("/api/stack/{action}")
    async def stack_action(action: str, payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal shutdown_result
        if action not in {"start", "stop"}:
            return {"ok": False, "error": "Unknown lifecycle action"}
        if payload.get("confirmed") is not True:
            return {"ok": False, "error": "Human confirmation is required"}
        save = payload.get("save", True)
        if type(save) is not bool:
            return {"ok": False, "error": "save must be a boolean"}
        if not save and payload.get("confirm_without_save") is not True:
            return {"ok": False, "error": "Confirm stopping without saving separately"}
        if lifecycle_lock.locked():
            return {"ok": False, "error": "A stack lifecycle operation is already running"}
        try:
            token = payload.get("overwrite_token")
            if token is not None and not isinstance(token, str):
                return {"ok": False, "error": "Invalid overwrite confirmation"}
            use_existing_map = payload.get("use_existing_map", False)
            if type(use_existing_map) is not bool:
                return {"ok": False, "error": "use_existing_map must be a boolean"}
            async with lifecycle_lock:
                if action == "start":
                    result = (
                        await asyncio.to_thread(runtime.start, token, use_existing_map=True)
                        if use_existing_map
                        else await asyncio.to_thread(runtime.start, token)
                    )
                    shutdown_result = None
                else:
                    if (
                        prepare_shutdown is not None
                        and runtime.profile.has_map
                        and runtime.status().get("pid")
                    ):
                        if shutdown_result is None:
                            acknowledgement = await prepare_shutdown(save)
                            if not acknowledgement.get("ok"):
                                return acknowledgement
                            details = acknowledgement.get("result")
                            if not isinstance(details, dict) or details.get("state") != (
                                "saved" if save else "not_saved"
                            ):
                                return {"ok": False, "error": "Invalid map save acknowledgement"}
                            shutdown_result = details
                    result = await asyncio.to_thread(runtime.stop)
                    if shutdown_result is not None:
                        result["map_save"] = shutdown_result
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except OSError:
            return {
                "ok": False,
                "error": "Stack lifecycle failed; check the stack logs and project directory",
            }
        return {"ok": True, **result}
