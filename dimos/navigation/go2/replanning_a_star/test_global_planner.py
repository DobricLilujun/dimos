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

from dimos.core.global_config import GlobalConfig
from dimos.msgs.geometry_msgs.Pose import Pose
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.nav_msgs.OccupancyGrid import OccupancyGrid
from dimos.msgs.nav_msgs.Path import Path
from dimos.navigation.go2.replanning_a_star.global_planner import GlobalPlanner


@pytest.fixture
def planner():
    global_planner = GlobalPlanner(GlobalConfig())
    yield global_planner
    global_planner.stop()


def test_cancel_while_path_is_computed_does_not_start_stale_navigation(planner, mocker):
    start = mocker.patch.object(planner._local_planner, "start_planning")
    mocker.patch.object(planner, "_find_safe_goal", return_value=Vector3(2, 0, 0))

    def cancel_while_finding_path(_goal, _position):
        planner.cancel_goal()
        return Path(poses=[PoseStamped(position=[0, 0, 0]), PoseStamped(position=[2, 0, 0])])

    mocker.patch.object(planner, "_find_wide_path", side_effect=cancel_while_finding_path)
    planner.handle_odom(PoseStamped(position=[0, 0, 0]))

    planner.handle_goal_request(PoseStamped(position=[2, 0, 0]))

    start.assert_not_called()


def test_find_wide_path_with_start_inside_inflation() -> None:
    """A wall observed at the last moment can be so close that its inflation
    covers the robot's own cell (the robot drove there before the costmap
    caught up). Planning must still find a way out instead of failing."""

    resolution = 0.05
    grid = np.zeros((60, 60), dtype=np.int8)
    grid[20:40, 30] = 100  # wall at x=1.5m spanning y=1.0..2.0m
    costmap = OccupancyGrid(grid=grid, resolution=resolution, origin=Pose(), frame_id="world")

    planner = GlobalPlanner(GlobalConfig())
    planner.handle_global_costmap(costmap)

    # 7 cm in front of the wall: within the inflation radius
    # (robot_width * 1.1 / 2 = 0.165m), so the start cell is engulfed.
    robot = Vector3(1.43, 1.5, 0)
    # On the other side of the wall; the path must round a wall end.
    goal = Vector3(2.75, 1.5, 0)

    path = planner._find_wide_path(goal, robot)

    assert path is not None
    assert len(path.poses) > 0
