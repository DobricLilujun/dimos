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

import numpy as np
import pytest

pytest.importorskip("open3d")

from dimos.simulation.mujoco.constants import MAX_RANGE
from dimos.simulation.mujoco.depth_camera import depth_image_to_point_cloud


def _wall(depth_m: float) -> np.ndarray:
    return np.full((36, 64), depth_m, dtype=np.float32)


def _cloud(depth_m: float, **kwargs: float) -> np.ndarray:
    return depth_image_to_point_cloud(
        _wall(depth_m), np.zeros(3), np.eye(3), fov_degrees=90, **kwargs
    )


def test_default_range_is_the_documented_three_metres() -> None:
    assert MAX_RANGE == 3


def test_surfaces_beyond_the_default_range_are_invisible() -> None:
    assert len(_cloud(5.0)) == 0


def test_surfaces_within_the_default_range_are_seen() -> None:
    assert len(_cloud(2.0)) > 0


def test_a_longer_range_sees_what_the_default_misses() -> None:
    cloud = _cloud(5.0, max_range=8.0)

    assert len(cloud) > 0
    # The camera looks down its own -z axis, so depth shows up as |z| after the flip.
    assert np.abs(cloud[:, 2]).max() == pytest.approx(5.0, abs=0.01)


def test_a_longer_range_still_has_a_limit() -> None:
    assert len(_cloud(9.0, max_range=8.0)) == 0


def test_a_longer_range_only_adds_points() -> None:
    default = {tuple(point) for point in np.round(_cloud(2.0), 6)}
    longer = {tuple(point) for point in np.round(_cloud(2.0, max_range=8.0), 6)}

    # The limit also applies sideways, so wide-angle points near the camera are added.
    assert default < longer
