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

from dimos.msgs.geometry_msgs.Pose import Pose
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Transform import Transform
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.sensor_msgs.PointCloud2 import PointCloud2
from dimos.navigation.go2.loop_closure.pgo_map import PGOMap

# TODO(PY311): drop. Skip before importing test_pgo, which skips mid-import without gtsam.
pytest.importorskip("gtsam")

from dimos.navigation.go2.loop_closure.test_pgo import _graph_with_drift_at

VOXEL = 0.05


class _FakePGO:
    """Stands in for the optimizer: the test decides when a loop closes."""

    n_keyframes = 0
    n_loops = 0
    graph = None

    def process(self, *args):
        pass

    def snapshot(self):
        return self.graph


def _wall(x, ts):
    ys, zs = np.meshgrid(np.arange(0.025, 1, VOXEL), np.arange(0.025, 1, VOXEL))
    pts = np.stack([np.full(ys.size, x), ys.ravel(), zs.ravel()], axis=1)
    return PointCloud2.from_numpy(pts, timestamp=ts)


def _xs(world_map):
    return {int(x) for x in np.floor(world_map.global_map().points_f32()[:, 0] / VOXEL)}


def test_rebuilds_on_loop_closure_and_respects_cooldown():
    world_map = PGOMap(rebuild_cooldown_s=10.0)
    world_map._pgo = pgo = _FakePGO()
    pose = PoseStamped(1.0, 0.0, 0.0)

    # the same wall seen twice, the first time 0.5 m off; no loop yet
    assert not world_map.add(_wall(1.025, 100.0), pose)
    assert not world_map.add(_wall(1.525, 101.0), pose)
    assert _xs(world_map) == {20, 30}

    # loop closes: the frame at t=100 was really 0.5 m further along x
    pgo.n_loops = 1
    pgo.graph = _graph_with_drift_at(
        [
            Transform(translation=Vector3(0.5, 0.0, 0.0), ts=100.0),
            Transform(translation=Vector3(0.0, 0.0, 0.0), ts=101.0),
        ]
    )
    assert world_map.add(_wall(1.525, 102.0), pose)
    assert _xs(world_map) == {30}

    # a second loop inside the cooldown waits for it
    pgo.n_loops = 2
    assert not world_map.add(_wall(1.525, 105.0), pose)
    assert world_map.add(_wall(1.525, 112.5), pose)


@pytest.fixture
def fixed_map(mocker):
    world_map = PGOMap(fixed_world=True, rebuild_cooldown_s=0)
    optimizer = mocker.patch.object(world_map, "_pgo")
    optimizer.n_keyframes = 2
    optimizer.n_loops = 1
    optimizer.snapshot.return_value = _graph_with_drift_at(
        [
            Transform(translation=Vector3(0, 0, 0), ts=100.0),
            Transform(translation=Vector3(0.5, 0, 0), ts=101.0),
        ]
    )
    yield world_map, optimizer
    world_map.dispose()


def test_fixed_world_keeps_start_and_corrects_new_scans_after_loop(fixed_map):
    world_map, optimizer = fixed_map
    assert optimizer.snapshot.return_value.world_correction(101.0).ts == 101.0
    world_map.add(_wall(1.025, 100.0), None)
    world_map.add(_wall(2.025, 101.0), None)
    assert _xs(world_map) == {20, 50}
    # Future raw scans need the same correction even without another accepted loop.
    world_map.add(_wall(3.025, 102.0), None)
    assert _xs(world_map) == {20, 50, 70}
    assert _xs(world_map) == {20, 50, 70}
    world_map, optimizer = fixed_map
    optimizer.n_loops = 2
    optimizer.snapshot.return_value = _graph_with_drift_at(
        [
            Transform(translation=Vector3(0, 0, 0), ts=100.0),
            Transform(translation=Vector3(0.25, 0, 0), ts=101.0),
        ]
    )
    assert world_map.flush()
    assert _xs(world_map) == {20, 45, 65}


def test_flush_applies_pending_loop_before_save(fixed_map):
    world_map, optimizer = fixed_map
    optimizer.n_loops = 0
    world_map.add(_wall(1.025, 101.0), None)
    optimizer.n_loops = 1
    assert world_map.flush()
    assert not world_map.flush()
    assert _xs(world_map) == {30}


def test_fixed_world_accepts_valid_starting_odometry_at_zero(fixed_map):
    world_map, optimizer = fixed_map
    world_map.add(_wall(1.025, 100.0), Pose(position=[0, 0, 0]))
    optimizer.process.assert_called_once()
    np.testing.assert_array_equal(optimizer.process.call_args.args[0].translation(), [0, 0, 0])
