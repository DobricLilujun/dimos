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

from pathlib import Path
from typing import Literal

from dimos.protocol.service.spec import BaseConfig
from dimos.simulation.mujoco.constants import MAX_RANGE


class MujocoConfig(BaseConfig):
    """Settings for the Unitree MuJoCo subprocess."""

    camera_position: tuple[float, float, float, float, float, float] = (
        -0.906,
        0.008,
        1.101,
        4.931,
        89.749,
        -46.378,
    )
    room: str = "office_555"
    room_from_occupancy: Path | None = None
    # A second simulated person walking a loop, used by the person-following demos.
    mujoco_second_person: bool = False
    # Cap on the simulated lidar range in metres; distant walls stay unobserved.
    mujoco_lidar_max_range: float = MAX_RANGE
    start_pos: tuple[float, float] = (-1.0, 1.0)
    steps_per_frame: int = 7
    # Auto disables shadows when rendering cannot keep up with realtime.
    shadows: Literal["auto", "on", "off"] = "auto"
