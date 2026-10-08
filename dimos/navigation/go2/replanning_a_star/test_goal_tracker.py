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

from collections.abc import Generator
import math

import pytest

from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.navigation.go2.replanning_a_star.goal_tracker import GoalTracker, GoalUpdatePolicy

ROBOT = Vector3(0.0, 0.0, 0.0)


def _policy(
    threshold: float = 0.5, interval: float = 0.0, follow_distance: float = 0.5
) -> GoalUpdatePolicy:
    return GoalUpdatePolicy(threshold, interval, follow_distance)


def test_first_target_stops_short_of_the_target_and_faces_it() -> None:
    goal = _policy().next_goal(Vector3(4.0, 0.0, 0.0), ROBOT, now=0.0)

    assert goal is not None
    assert goal.x == pytest.approx(3.5)
    assert goal.y == pytest.approx(0.0)
    assert goal.orientation.euler.z == pytest.approx(0.0)


def test_goal_points_along_the_line_from_the_robot() -> None:
    goal = _policy(follow_distance=1.0).next_goal(Vector3(0.0, 3.0, 0.0), ROBOT, now=0.0)

    assert goal is not None
    assert goal.x == pytest.approx(0.0)
    assert goal.y == pytest.approx(2.0)
    assert goal.orientation.euler.z == pytest.approx(math.pi / 2)


def test_zero_follow_distance_goes_to_the_target() -> None:
    goal = _policy(follow_distance=0.0).next_goal(Vector3(2.0, 1.0, 0.0), ROBOT, now=0.0)

    assert goal is not None
    assert (goal.x, goal.y) == pytest.approx((2.0, 1.0))


def test_movement_below_threshold_is_ignored() -> None:
    policy = _policy(threshold=0.5)
    assert policy.next_goal(Vector3(4.0, 0.0, 0.0), ROBOT, now=0.0) is not None

    assert policy.next_goal(Vector3(4.3, 0.0, 0.0), ROBOT, now=1.0) is None


def test_movement_at_threshold_sends_a_new_goal() -> None:
    policy = _policy(threshold=0.5)
    policy.next_goal(Vector3(4.0, 0.0, 0.0), ROBOT, now=0.0)

    goal = policy.next_goal(Vector3(4.0, 0.5, 0.0), ROBOT, now=1.0)

    assert goal is not None


def test_small_steps_add_up_against_the_last_sent_target() -> None:
    policy = _policy(threshold=0.5)
    policy.next_goal(Vector3(4.0, 0.0, 0.0), ROBOT, now=0.0)

    assert policy.next_goal(Vector3(4.2, 0.0, 0.0), ROBOT, now=1.0) is None
    assert policy.next_goal(Vector3(4.4, 0.0, 0.0), ROBOT, now=2.0) is None
    assert policy.next_goal(Vector3(4.6, 0.0, 0.0), ROBOT, now=3.0) is not None


def test_robot_motion_alone_does_not_trigger_an_update() -> None:
    policy = _policy()
    policy.next_goal(Vector3(4.0, 0.0, 0.0), ROBOT, now=0.0)

    assert policy.next_goal(Vector3(4.0, 0.0, 0.0), Vector3(2.0, 0.0, 0.0), now=1.0) is None


def test_updates_are_rate_limited_and_the_next_one_uses_the_latest_target() -> None:
    policy = _policy(threshold=0.5, interval=1.0)
    policy.next_goal(Vector3(4.0, 0.0, 0.0), ROBOT, now=0.0)

    assert policy.next_goal(Vector3(5.0, 0.0, 0.0), ROBOT, now=0.5) is None
    goal = policy.next_goal(Vector3(6.0, 0.0, 0.0), ROBOT, now=1.0)

    assert goal is not None
    assert goal.x == pytest.approx(5.5)


def test_no_goal_when_the_robot_is_already_inside_the_follow_distance() -> None:
    assert _policy().next_goal(Vector3(0.6, 0.0, 0.0), ROBOT, now=0.0) is None


def test_goal_is_sent_once_the_target_leaves_the_follow_distance() -> None:
    policy = _policy()
    assert policy.next_goal(Vector3(0.6, 0.0, 0.0), ROBOT, now=0.0) is None

    assert policy.next_goal(Vector3(1.5, 0.0, 0.0), ROBOT, now=1.0) is not None


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_non_finite_target_is_ignored(bad: float) -> None:
    assert _policy().next_goal(Vector3(bad, 0.0, 0.0), ROBOT, now=0.0) is None


def test_reset_makes_the_next_target_send_a_goal() -> None:
    policy = _policy()
    policy.next_goal(Vector3(4.0, 0.0, 0.0), ROBOT, now=0.0)
    policy.reset()

    assert policy.next_goal(Vector3(4.0, 0.0, 0.0), ROBOT, now=0.1) is not None


@pytest.fixture
def tracker() -> Generator[tuple[GoalTracker, list[PoseStamped]], None, None]:
    module = GoalTracker(enabled=True, min_update_interval_s=0.0)
    goals: list[PoseStamped] = []
    unsub = module.goal_request.subscribe(goals.append)
    try:
        yield module, goals
    finally:
        unsub()
        module._close_module()


def _pose(x: float, y: float) -> PoseStamped:
    return PoseStamped(frame_id="world", position=Vector3(x, y, 0.0))


def test_module_publishes_goals_only_when_the_target_moves(tracker) -> None:  # type: ignore[no-untyped-def]
    module, goals = tracker
    module._on_odom(_pose(0.0, 0.0))

    module._on_target(_pose(4.0, 0.0))
    module._on_target(_pose(4.1, 0.0))
    module._on_target(_pose(5.0, 0.0))

    assert [round(goal.x, 2) for goal in goals] == [3.5, 4.5]


def test_module_waits_for_odometry(tracker) -> None:  # type: ignore[no-untyped-def]
    module, goals = tracker

    module._on_target(_pose(4.0, 0.0))

    assert goals == []


def test_module_is_inert_unless_enabled() -> None:
    module = GoalTracker()
    goals: list[PoseStamped] = []
    unsub = module.goal_request.subscribe(goals.append)
    try:
        module._on_odom(_pose(0.0, 0.0))
        module._on_target(_pose(4.0, 0.0))

        assert goals == []

        module.start_tracking()
        module._on_target(_pose(4.0, 0.0))

        assert len(goals) == 1
    finally:
        unsub()
        module._close_module()


def test_stop_tracking_stops_goals_and_cancels_the_planner(tracker, mocker) -> None:  # type: ignore[no-untyped-def]
    module, goals = tracker
    planner = mocker.Mock()
    module._planner = planner
    module._on_odom(_pose(0.0, 0.0))

    module.stop_tracking()
    module._on_target(_pose(4.0, 0.0))

    planner.cancel_goal.assert_called_once_with()
    assert goals == []


def test_update_target_rpc_feeds_the_same_path(tracker) -> None:  # type: ignore[no-untyped-def]
    module, goals = tracker
    module._on_odom(_pose(0.0, 0.0))

    module.update_target(3.0, 4.0)

    assert len(goals) == 1
    assert (goals[0].x, goals[0].y) == pytest.approx((2.7, 3.6))
