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

import math

from pydantic import ValidationError
import pytest

from dimos.mapping.relocalization.go2.fusion_gate import FusionGateConfig, FusionMotionGate
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Twist import Twist
from dimos.msgs.geometry_msgs.Vector3 import Vector3


def pose(x=0.0, yaw=0.0, sign=1.0):
    half = math.radians(yaw) / 2
    return PoseStamped(
        frame_id="world",
        position=[x, 0, 0],
        orientation=[0, 0, sign * math.sin(half), sign * math.cos(half)],
    )


@pytest.fixture
def gate():
    return FusionMotionGate(FusionGateConfig())


def test_continuous_small_drift_does_not_reenable_stationary_fusion(gate):
    for i in range(481):
        t = i * 0.125
        gate.on_pose(pose(x=t * 0.01, yaw=t), t)
    assert gate.state == "stationary"
    assert gate.permits_fusion(60.0) is False
    assert gate.speed == pytest.approx(0.01)
    assert gate.rotation_deg == pytest.approx(1.0)


@pytest.mark.parametrize("speed,rotation", [(0.1, 0), (0, 10), (0.03, 0)])
def test_translation_turning_and_slow_motion_keep_fusing(gate, speed, rotation):
    for i in range(21):
        t = i * 0.125
        gate.on_pose(pose(x=t * speed, yaw=t * rotation), t)
    assert gate.permits_fusion(2.5) is True


def test_hysteresis_resumes_only_above_resume_threshold(gate):
    for i in range(21):
        gate.on_pose(pose(), i * 0.125)
    for i in range(1, 9):
        gate.on_pose(pose(x=i * 0.125 * 0.03), 2.5 + i * 0.125)
    assert gate.permits_fusion(3.5) is False
    for i in range(1, 9):
        gate.on_pose(pose(x=0.03 + i * 0.125 * 0.1), 3.5 + i * 0.125)
    assert gate.state == "moving"
    assert gate.permits_fusion(4.5) is True


def test_exact_stop_speed_enters_stationary_and_exact_resume_stays_paused():
    gate = FusionMotionGate(FusionGateConfig(fusion_stop_speed=0.125, fusion_resume_speed=0.25))
    for i in range(21):
        gate.on_pose(pose(x=i * 0.125 * 0.125), i * 0.125)
    assert gate.state == "stationary"
    for i in range(1, 9):
        gate.on_pose(pose(x=2.5 * 0.125 + i * 0.125 * 0.25), 2.5 + i * 0.125)
    assert gate.permits_fusion(3.5) is False


def test_quaternion_sign_and_yaw_wrap_do_not_create_false_motion(gate):
    for i in range(21):
        yaw = 179 + i * 0.125
        gate.on_pose(pose(yaw=yaw if yaw <= 180 else yaw - 360, sign=(-1) ** i), i * 0.125)
    assert gate.state == "stationary"
    assert gate.rotation_deg == pytest.approx(1)


def test_stale_and_invalid_odometry_require_a_new_window(gate):
    gate.on_pose(pose(), 0)
    assert gate.permits_fusion(0) is False
    assert gate.fresh_pose(0) is True
    gate.on_pose(pose(x=0.1), 0.5)
    assert gate.permits_fusion(0.5) is True
    assert gate.permits_fusion(2) is False
    assert "stale" in gate.reason
    gate.on_pose(pose(), 2.1)
    assert gate.permits_fusion(2.1) is False
    assert gate.on_pose(pose(x=float("nan")), 2.2) is False
    assert gate.fresh_pose(2.2) is False
    invalid = pose()
    invalid.orientation.w = 0
    assert gate.on_pose(invalid, 2.3) is False
    assert gate.state == "unknown"


def test_recent_commands_delay_static_detection_but_expire(gate):
    for i in range(21):
        t = i * 0.125
        gate.on_command(Twist(linear=Vector3(0.1, 0, 0)), t)
        gate.on_pose(pose(), t)
    assert gate.state == "settling"
    for i in range(21, 45):
        gate.on_pose(pose(), i * 0.125)
    assert gate.state == "stationary"
    gate.on_command(Twist(linear=Vector3(0.1, 0, 0)), 5.625)
    gate.on_pose(pose(), 5.625)
    assert gate.permits_fusion(5.625) is False


@pytest.mark.parametrize(
    "config",
    [
        {"fusion_window": 0},
        {"fusion_stationary_duration": float("nan")},
        {"fusion_stop_speed": -1},
        {"fusion_resume_speed": 0.02},
        {"fusion_resume_rotation_deg": 2},
        {"fusion_sensor_timeout": 0.1},
        {"fusion_window": float("inf")},
    ],
)
def test_invalid_gate_parameters_are_rejected(config):
    with pytest.raises(ValidationError):
        FusionGateConfig(**config)
