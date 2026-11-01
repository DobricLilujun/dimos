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

"""Low-speed odometry heuristic; not an independent stationary measurement."""

from collections import deque
import math
import sys
from typing import Literal

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

from pydantic import BaseModel, Field, model_validator

from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Twist import Twist


class FusionGateConfig(BaseModel):
    auto_pause_fusion: bool = False
    fusion_window: float = Field(default=0.5, gt=0, allow_inf_nan=False)
    fusion_stationary_duration: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    fusion_sensor_timeout: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    fusion_stop_speed: float = Field(default=0.02, ge=0, allow_inf_nan=False)
    fusion_resume_speed: float = Field(default=0.04, gt=0, allow_inf_nan=False)
    fusion_stop_rotation_deg: float = Field(default=2.0, ge=0, allow_inf_nan=False)
    fusion_resume_rotation_deg: float = Field(default=3.0, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_fusion_thresholds(self) -> Self:
        if self.fusion_resume_speed <= self.fusion_stop_speed:
            raise ValueError("Fusion resume speed must exceed stop speed")
        if self.fusion_resume_rotation_deg <= self.fusion_stop_rotation_deg:
            raise ValueError("Fusion resume rotation must exceed stop rotation")
        if self.fusion_sensor_timeout < self.fusion_window:
            raise ValueError("Fusion sensor timeout must cover the motion window")
        return self


class FusionMotionGate:
    def __init__(self, config: FusionGateConfig) -> None:
        self.config = config
        self._samples: deque[
            tuple[float, tuple[float, float, float], tuple[float, float, float, float]]
        ] = deque(maxlen=512)
        self._low_since: float | None = None
        self._command_until = -math.inf
        self.state: Literal["unknown", "settling", "moving", "stationary"] = "unknown"
        self.reason = "Waiting for odometry"
        self.speed: float | None = None
        self.rotation_deg: float | None = None

    def _reset(self, reason: str) -> None:
        self._samples.clear()
        self._low_since = None
        self.speed = self.rotation_deg = None
        self.state = "unknown"
        self.reason = reason

    def on_command(self, command: Twist, now: float) -> None:
        values = (
            command.linear.x,
            command.linear.y,
            command.linear.z,
            command.angular.x,
            command.angular.y,
            command.angular.z,
        )
        if all(math.isfinite(value) for value in values) and any(values):
            self._command_until = now + self.config.fusion_sensor_timeout
            self._low_since = None

    def on_pose(self, pose: PoseStamped, now: float) -> bool:
        position = (pose.position.x, pose.position.y, pose.position.z)
        quaternion = pose.orientation.to_tuple()
        norm = math.hypot(*quaternion)
        if (
            pose.frame_id != "world"
            or not all(math.isfinite(value) for value in (*position, *quaternion))
            or not math.isfinite(norm)
            or norm < 1e-8
        ):
            self._reset("Invalid world-frame odometry")
            return False
        if self._samples:
            dt = now - self._samples[-1][0]
            if dt <= 0 or dt > self.config.fusion_sensor_timeout:
                self._reset("Odometry interrupted; rebuilding motion window")
        normalized = tuple(value / norm for value in quaternion)
        self._samples.append(
            (now, position, (normalized[0], normalized[1], normalized[2], normalized[3]))
        )
        while len(self._samples) > 2 and self._samples[1][0] <= now - self.config.fusion_window:
            self._samples.popleft()
        start, previous, rotation = self._samples[0]
        dt = now - start
        if dt < self.config.fusion_window:
            self.state = "unknown"
            self.reason = "Collecting motion window"
            return True
        self.speed = math.dist(position, previous) / dt
        dot = abs(sum(a * b for a, b in zip(normalized, rotation, strict=True)))
        self.rotation_deg = math.degrees(2 * math.acos(min(1.0, dot))) / dt
        if not math.isfinite(self.speed) or not math.isfinite(self.rotation_deg):
            self._reset("Invalid odometry motion rate")
            return False
        moving = (
            self.speed > self.config.fusion_resume_speed
            or self.rotation_deg > self.config.fusion_resume_rotation_deg
        )
        low = (
            self.speed <= self.config.fusion_stop_speed
            and self.rotation_deg <= self.config.fusion_stop_rotation_deg
        )
        commanded = now < self._command_until
        if moving:
            self.state, self.reason = "moving", "Motion detected"
            self._low_since = None
        elif self.state == "stationary":
            # Commands alone cannot prove motion, including commands to a blocked robot.
            self.reason = "Low-speed odometry; stationary fusion paused"
        elif low and not commanded:
            if self._low_since is None:
                self._low_since = now
            if now - self._low_since >= self.config.fusion_stationary_duration:
                self.state, self.reason = (
                    "stationary",
                    "Low-speed odometry; stationary fusion paused",
                )
            else:
                self.state, self.reason = "settling", "Waiting for stationary dwell"
        else:
            self._low_since = None
            self.state, self.reason = "settling", "Motion uncertain or recent movement command"
        return True

    def fresh_pose(self, now: float) -> bool:
        if not self._samples:
            return False
        age = now - self._samples[-1][0]
        if age < 0 or age > self.config.fusion_sensor_timeout:
            self._reset("Odometry stale; fusion paused")
            return False
        return True

    def permits_fusion(self, now: float) -> bool:
        return self.fresh_pose(now) and self.state in {"moving", "settling"}
