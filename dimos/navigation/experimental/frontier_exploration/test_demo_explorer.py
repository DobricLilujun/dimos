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

import threading
import time

from dimos_lcm.std_msgs import Bool
import numpy as np
from pydantic import ValidationError
import pytest

from dimos.msgs.geometry_msgs.Pose import Pose
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.nav_msgs.OccupancyGrid import OccupancyGrid
from dimos.navigation.experimental.frontier_exploration.demo_explorer import (
    DemoExplorer,
    ExploreOptions,
    reachable_distances,
)


def grid(values, resolution=1.0):
    return OccupancyGrid(
        grid=np.array(values, dtype=np.int8),
        resolution=resolution,
        origin=Pose(),
        frame_id="world",
    )


@pytest.fixture
def explorer(mocker):
    module = DemoExplorer()
    mocker.patch.object(module, "_navigation", create=True)
    mocker.patch.object(module.exploration_state, "publish")
    mocker.patch.object(module, "start_tool")
    mocker.patch.object(module, "stop_tool")
    module._on_costmap(grid(np.zeros((10, 10), dtype=np.int8)))
    module._on_odometry(PoseStamped(position=[1, 1, 0]))
    yield module
    module.stop_exploration()
    module._close_module()


def test_dijkstra_uses_actual_detour_distance_and_rejects_disconnected_cells():
    values = np.zeros((7, 8), dtype=np.int8)
    values[:5, 3] = 100
    values[:, 6] = 100
    distances = reachable_distances(grid(values), Vector3(1, 1, 0))
    assert distances[1, 4] > 8
    assert np.isinf(distances[1, 7])


def test_dijkstra_never_cuts_blocked_diagonal_corner():
    distances = reachable_distances(grid([[0, 100], [100, 0]]), Vector3(0, 0, 0))
    assert np.isinf(distances[1, 1])


@pytest.mark.parametrize("strategy", ["frontier", "efficient"])
def test_frontier_goal_is_reachable_free_space_not_unknown_centroid(explorer, strategy):
    explorer.options = ExploreOptions(strategy=strategy)
    values = np.zeros((9, 12), dtype=np.int8)
    values[:, 9:] = -1
    costmap = grid(values)
    goal = explorer.select_goal(Vector3(1, 4, 0), costmap)
    assert goal is not None
    cell = costmap.world_to_grid(goal)
    assert costmap.grid[int(cell.y), int(cell.x)] == 0
    assert np.isfinite(reachable_distances(costmap, Vector3(1, 4, 0))[int(cell.y), int(cell.x)])


def test_efficient_prefers_near_frontier_over_equal_size_distant_frontier(explorer):
    explorer.options = ExploreOptions(strategy="efficient")
    values = np.zeros((7, 20), dtype=np.int8)
    values[:, 0] = -1
    values[:, 19] = -1
    goal = explorer.select_goal(Vector3(4, 3, 0), grid(values))
    assert goal is not None
    assert goal.x < 4


def test_reached_frontier_does_not_hide_remaining_cluster_coverage(explorer):
    values = np.zeros((12, 12), dtype=np.int8)
    values[:, 10:] = -1
    costmap = grid(values)
    pose = Vector3(1, 5, 0)
    first = explorer.select_goal(pose, costmap)
    assert first is not None
    explorer.mark_explored_goal(first)
    second = explorer.select_goal(pose, costmap)
    assert second is not None
    assert math_distance(first, second) >= 0.75


def math_distance(a, b):
    return float(np.hypot(a.x - b.x, a.y - b.y))


def test_failed_goal_cooldown_excludes_region_then_expires(explorer, mocker):
    values = np.zeros((5, 12), dtype=np.int8)
    values[:, 10:] = -1
    costmap, pose = grid(values), Vector3(1, 2, 0)
    first = explorer.select_goal(pose, costmap)
    assert first is not None
    explorer._failed = [(first, 100)]
    mocker.patch(
        "dimos.navigation.experimental.frontier_exploration.demo_explorer.time.monotonic",
        return_value=50,
    )
    next_goal = explorer.select_goal(pose, costmap)
    assert next_goal is not None
    assert math_distance(first, next_goal) >= 0.75
    explorer._failed = [(first, 49)]
    assert explorer.select_goal(pose, costmap) == first


def test_low_gain_stops_after_ten_successful_goals_and_two_low_gain_arrivals(explorer):
    before = explorer.latest_costmap
    for i in range(10):
        assert explorer._record_arrival(Vector3(i, 0, 0), before) is False
    assert explorer._record_arrival(Vector3(10, 0, 0), before) is True
    assert explorer.exploration_status()["phase"] == "completed"


def test_custom_goal_warmup_and_gain_percent_are_used_exactly(explorer):
    explorer.options = ExploreOptions(min_goals=2, gain_percent=1, no_gain_attempts=1)
    before = grid(np.zeros((10, 10), dtype=np.int8))
    explorer._on_costmap(grid(np.zeros((10, 11), dtype=np.int8)))
    assert explorer._record_arrival(Vector3(0, 0, 0), before) is False
    assert explorer._record_arrival(Vector3(1, 0, 0), before) is False
    assert explorer.no_gain_counter == 0
    assert explorer._record_arrival(Vector3(2, 0, 0), explorer.latest_costmap) is True


def test_navigation_failure_wakes_immediately_without_three_second_wait(explorer):
    explorer._navigation.set_goal.side_effect = lambda goal: (
        explorer._on_goal_reached(Bool(False)) or True
    )
    started = time.monotonic()
    assert explorer._navigate(Vector3(4, 2, 0)) is False
    assert time.monotonic() - started < 1
    assert explorer.explored_goals == []
    explorer._navigation.cancel_goal.assert_called_once()


def test_check_interval_is_not_a_hard_navigation_deadline(explorer, mocker):
    calls = []

    def progress(interval):
        calls.append(interval)
        explorer._on_odometry(PoseStamped(position=[1 + len(calls) * 0.2, 1, 0]))
        if len(calls) == 3:
            explorer._on_goal_reached(Bool(True))
        return True

    mocker.patch.object(explorer._wake, "wait", side_effect=progress)
    assert explorer._navigate(Vector3(4, 1, 0)) is True
    assert calls == [3, 3, 3]
    explorer._navigation.cancel_goal.assert_not_called()


def test_stop_wakes_waiter_and_does_not_report_arrival(explorer, mocker):
    mocker.patch.object(
        explorer._wake, "wait", side_effect=lambda interval: explorer.stop_exploration()
    )
    explorer.exploration_active = True
    assert explorer._navigate(Vector3(4, 1, 0)) is False
    assert explorer.explored_goals == []
    assert explorer.stop_event.is_set()


def test_stale_sensor_cancels_active_navigation(explorer, mocker):
    mocker.patch.object(explorer._wake, "wait", return_value=False)
    explorer._odom_received = 0
    assert explorer._navigate(Vector3(4, 1, 0)) is False
    assert explorer.exploration_status()["reason"] == "Odometry is stale or no map is available"
    explorer._navigation.cancel_goal.assert_called_once()


def test_stop_during_selection_cannot_launch_late_goal(explorer, mocker):
    def choose(pose, costmap):
        explorer.stop_event.set()
        return Vector3(4, 1, 0)

    mocker.patch.object(explorer, "select_goal", side_effect=choose)
    explorer._run_exploration_loop()
    explorer._navigation.set_goal.assert_not_called()


def test_paused_map_fusion_keeps_static_map_usable_with_fresh_odometry(explorer, mocker):
    start = time.monotonic()
    mocker.patch(
        "dimos.navigation.experimental.frontier_exploration.demo_explorer.time.monotonic",
        return_value=start + 30,
    )
    explorer._on_odometry(PoseStamped(position=[2, 1, 0]))
    assert explorer._sensors_fresh() is True
    explorer.latest_costmap = None
    assert explorer._sensors_fresh() is False


def test_failure_retries_another_goal_without_recording_success(explorer, mocker):
    goal = Vector3(4, 1, 0)

    def choose(pose, costmap):
        if explorer._failed:
            explorer.stop_event.set()
            return None
        return goal

    mocker.patch.object(explorer, "select_goal", side_effect=choose)
    mocker.patch.object(explorer, "_navigate", return_value=False)
    explorer._run_exploration_loop()
    assert explorer._failed[0][0] == goal
    assert explorer.explored_goals == []


def test_no_frontiers_has_bounded_explicit_completion(explorer, mocker):
    mocker.patch.object(explorer, "select_goal", return_value=None)
    mocker.patch.object(explorer.stop_event, "wait", return_value=False)
    explorer._run_exploration_loop()
    assert (
        explorer.exploration_status()["reason"] == "No eligible reachable frontiers after 10 checks"
    )
    assert explorer.exploration_active is False


@pytest.mark.parametrize(
    "values",
    [
        {"strategy": "bad"},
        {"min_goals": -1},
        {"gain_percent": float("nan")},
        {"gain_percent": 101},
        {"no_gain_attempts": 0},
        {"check_interval": 0},
    ],
)
def test_invalid_options_are_rejected_before_movement(explorer, values):
    with pytest.raises(ValidationError):
        explorer.begin_demo_exploration(**values)
    explorer._navigation.set_goal.assert_not_called()
    explorer.start_tool.assert_not_called()


def test_start_stop_owns_worker_and_releases_skill_hold(explorer, mocker):
    entered = threading.Event()

    def run():
        entered.set()
        explorer.stop_event.wait(5)

    mocker.patch.object(explorer, "_run_exploration_loop", side_effect=run)
    result = explorer.begin_demo_exploration(strategy="efficient", min_goals=12, check_interval=2)
    assert entered.wait(2)
    assert "efficient" in result
    assert (
        explorer.begin_demo_exploration()
        == "Exploration is already active or stopping; stop it before restarting."
    )
    assert explorer.options.min_goals == 12
    explorer.end_demo_exploration()
    assert not explorer.exploration_thread.is_alive()
    explorer.stop_tool.assert_any_call("begin_demo_exploration")
