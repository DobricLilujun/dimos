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

import pytest

from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.simulation.mujoco.person_target import (
    DEFAULT_TRACK,
    MujocoPersonTarget,
    TrackWalker,
)

SQUARE = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]


def test_walker_starts_at_the_first_waypoint_facing_the_second() -> None:
    walker = TrackWalker(SQUARE, speed_mps=1.0)

    assert walker.position == (0.0, 0.0)
    assert walker.heading == pytest.approx(0.0)


def test_walker_moves_at_constant_speed_along_a_segment() -> None:
    walker = TrackWalker(SQUARE, speed_mps=0.5)

    walker.advance(2.0)

    assert walker.position == pytest.approx((1.0, 0.0))


def test_walker_turns_at_a_waypoint_and_keeps_the_leftover_distance() -> None:
    walker = TrackWalker(SQUARE, speed_mps=1.0)

    walker.advance(2.5)

    assert walker.position == pytest.approx((2.0, 0.5))
    assert walker.heading == pytest.approx(math.pi / 2)


def test_walker_closes_the_loop() -> None:
    walker = TrackWalker(SQUARE, speed_mps=1.0)

    walker.advance(8.0)

    assert walker.position == pytest.approx((0.0, 0.0))


def test_walker_steps_over_a_repeated_waypoint() -> None:
    walker = TrackWalker([(0.0, 0.0), (1.0, 0.0), (1.0, 0.0), (1.0, 1.0)], speed_mps=1.0)

    walker.advance(1.5)

    assert walker.position == pytest.approx((1.0, 0.5))


@pytest.mark.parametrize("track", [[], [(0.0, 0.0)], [(1.0, 1.0), (1.0, 1.0)]])
def test_walker_rejects_tracks_without_a_path(track: list[tuple[float, float]]) -> None:
    with pytest.raises(ValueError):
        TrackWalker(track, speed_mps=1.0)


def test_default_track_is_a_valid_loop() -> None:
    walker = TrackWalker(DEFAULT_TRACK, speed_mps=0.25)

    walker.advance(1.0)

    assert math.dist(walker.position, DEFAULT_TRACK[0]) == pytest.approx(0.25)


@pytest.fixture
def person_target(mocker):  # type: ignore[no-untyped-def]
    transports: dict[str, object] = {}
    mocker.patch(
        "dimos.simulation.mujoco.person_target.make_transport",
        side_effect=lambda topic, _type: transports.setdefault(topic, mocker.Mock()),
    )
    modules: list[MujocoPersonTarget] = []

    def make(**config):  # type: ignore[no-untyped-def]
        module = MujocoPersonTarget(**config)
        modules.append(module)
        return module

    yield make

    for module in modules:
        module._close_module()


def _run_ticks(module: MujocoPersonTarget, ticks: int, mocker) -> list[PoseStamped]:  # type: ignore[no-untyped-def]
    targets: list[PoseStamped] = []
    unsub = module.tracked_target.subscribe(targets.append)
    waits = 0

    def stop_after_ticks(_period: float) -> bool:
        nonlocal waits
        waits += 1
        if waits >= ticks:
            module._stop_event.set()
        return module._stop_event.is_set()

    mocker.patch.object(module._stop_event, "wait", side_effect=stop_after_ticks)
    try:
        module._run()
    finally:
        unsub()
    return targets


def test_person_pose_is_published_and_target_follows_once_walking(person_target, mocker) -> None:  # type: ignore[no-untyped-def]
    module = person_target(start_delay_s=0.0)

    targets = _run_ticks(module, 3, mocker)

    assert len(targets) >= 1
    assert all(target.frame_id == "world" for target in targets)
    assert module._person_pose.broadcast.call_count >= len(targets)


def test_target_is_withheld_during_the_start_delay(person_target, mocker) -> None:  # type: ignore[no-untyped-def]
    module = person_target(start_delay_s=3600.0)

    targets = _run_ticks(module, 3, mocker)

    assert targets == []
    assert module._person_pose.broadcast.call_count >= 1


def test_publish_target_can_be_turned_off_while_the_person_still_walks(
    person_target, mocker
) -> None:  # type: ignore[no-untyped-def]
    module = person_target(start_delay_s=0.0, publish_target=False)

    targets = _run_ticks(module, 3, mocker)

    assert targets == []
    assert module._person_pose.broadcast.call_count >= 3


def test_second_person_walks_on_its_own_topic_and_is_never_the_target(
    person_target, mocker
) -> None:  # type: ignore[no-untyped-def]
    track = [(10.0, 10.0), (12.0, 10.0), (12.0, 12.0)]
    module = person_target(start_delay_s=0.0, second_person_track=track)

    targets = _run_ticks(module, 3, mocker)

    assert module._second_person_pose is not module._person_pose
    assert module._second_person_pose.broadcast.call_count >= 3
    assert all(target.x < 5 for target in targets)
    poses = [call.args[1].position for call in module._second_person_pose.broadcast.call_args_list]
    assert all(pose.x >= 10.0 for pose in poses)


def test_there_is_no_second_transport_without_a_second_track(person_target) -> None:  # type: ignore[no-untyped-def]
    assert person_target()._second_person_pose is None
