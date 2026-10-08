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

from collections.abc import Callable, Generator
from dataclasses import dataclass
import json
from threading import Event
import time
from typing import Any

import numpy as np
import pytest

from dimos.agents.skills.person_navigation import (
    PersonNavigationSkillContainer,
    _parse_bbox,
    _Sighting,
)
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.sensor_msgs.Image import Image, ImageFormat
from dimos.types.robot_location import RobotLocation

WHITE = "white t-shirt"


def _person_json(bbox: list[int] | None) -> str:
    return json.dumps({"bbox": bbox})


@dataclass
class Rig:
    module: PersonNavigationSkillContainer
    vlm: Any
    memory: Any
    navigation: Any
    tracker: Any
    tags: list[RobotLocation]


@pytest.fixture
def make_rig(mocker) -> Generator[Callable[..., Rig], None, None]:  # type: ignore[no-untyped-def]
    modules: list[PersonNavigationSkillContainer] = []

    def make(**config: Any) -> Rig:
        module = PersonNavigationSkillContainer(**config)
        modules.append(module)
        tags: list[RobotLocation] = []
        image = Image.from_numpy(np.zeros((360, 640, 3), np.uint8), format=ImageFormat.BGR)

        memory = mocker.Mock()
        memory.capture_object_observation.return_value = (image, {"context": True})
        memory.locate_in_observation.return_value = {
            "position": [3.0, 1.0, 0.9],
            "method": "pointcloud_bbox",
            "point_count": 50,
        }
        memory.get_robot_locations.side_effect = lambda: list(tags)

        def add_named_location(name, position, rotation, description, kind="location", **_):  # type: ignore[no-untyped-def]
            tags.append(
                RobotLocation(
                    name=name,
                    position=tuple(position),
                    rotation=tuple(rotation),
                    metadata={"kind": kind, "description": description},
                )
            )
            return True

        memory.add_named_location.side_effect = add_named_location
        memory.update_robot_location.return_value = True

        vlm = mocker.Mock()
        vlm.query.return_value = _person_json([100, 50, 200, 300])
        module._vl_model = vlm
        module._spatial_memory = memory
        module._navigation = mocker.Mock()
        module._navigation.set_goal.return_value = True
        module._goal_tracker = mocker.Mock()
        module._on_odom(PoseStamped(frame_id="world", position=Vector3(0.0, 0.0, 0.0)))
        # Tool streams only exist inside a running McpServer call.
        mocker.patch.object(module, "start_tool")
        mocker.patch.object(module, "tool_update")
        mocker.patch.object(module, "stop_tool")
        return Rig(module, vlm, memory, module._navigation, module._goal_tracker, tags)

    yield make

    for module in modules:
        module._halt_following()
        module._close_module()


def test_default_model_is_gpt_5_6_luna() -> None:
    module = PersonNavigationSkillContainer()
    try:
        assert module.config.vlm_model == "gpt-5.6-luna"
        assert module._vl_model.config.model_name == "gpt-5.6-luna"
    finally:
        module._close_module()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ([1, 2, 30.4, 40.6], [1, 2, 30, 41]),
        (None, None),
        ([1, 2, 3], None),
        ([10, 10, 5, 20], None),
        ([0, 0, float("nan"), 5], None),
        (["a", 0, 1, 1], None),
        ("bbox", None),
    ],
)
def test_parse_bbox(raw: Any, expected: list[int] | None) -> None:
    assert _parse_bbox(raw) == expected


def test_tag_person_saves_a_person_tag_at_the_lidar_position(make_rig) -> None:  # type: ignore[no-untyped-def]
    rig = make_rig()

    result = rig.module.tag_person(WHITE)

    assert "3.00, 1.00" in result
    (tag,) = rig.tags
    assert tag.name == f"person: {WHITE}"
    assert tag.metadata["kind"] == "person"
    assert tag.position == (3.0, 1.0, 0.9)
    # The reference photo is the crop inside the box.
    crop = rig.memory.add_named_location.call_args.kwargs["reference_image"]
    assert crop.shape[:2] == (250, 100)
    prompt = rig.vlm.query.call_args.args[1]
    assert WHITE in prompt


def test_tagging_the_same_description_again_moves_the_tag(make_rig) -> None:  # type: ignore[no-untyped-def]
    rig = make_rig()
    rig.module.tag_person(WHITE)
    rig.memory.locate_in_observation.return_value = {
        "position": [4.0, 2.0, 0.9],
        "method": "pointcloud_bbox",
        "point_count": 40,
    }

    rig.module.tag_person(WHITE)

    assert len(rig.tags) == 1
    rig.memory.update_robot_location.assert_called_once_with(
        rig.tags[0].location_id, [4.0, 2.0, 0.9]
    )


def test_different_descriptions_make_different_tags(make_rig) -> None:  # type: ignore[no-untyped-def]
    rig = make_rig()

    rig.module.tag_person(WHITE)
    rig.module.tag_person("beige trousers")

    assert sorted(tag.name for tag in rig.tags) == ["person: beige trousers", f"person: {WHITE}"]


def test_tag_person_reports_when_nobody_matches(make_rig) -> None:  # type: ignore[no-untyped-def]
    rig = make_rig()
    rig.vlm.query.return_value = _person_json(None)

    result = rig.module.tag_person(WHITE)

    assert "No person matching" in result
    assert "describe_visible_people" in result
    assert rig.tags == []


def test_a_person_without_lidar_depth_is_not_tagged(make_rig) -> None:  # type: ignore[no-untyped-def]
    rig = make_rig()
    rig.memory.locate_in_observation.return_value = None

    assert "No person matching" in rig.module.tag_person(WHITE)
    assert rig.tags == []


def test_describe_visible_people_lists_everyone_with_positions(make_rig) -> None:  # type: ignore[no-untyped-def]
    rig = make_rig()
    rig.vlm.query.return_value = json.dumps(
        {
            "people": [
                {"description": "white t-shirt, dark trousers", "bbox": [10, 10, 60, 200]},
                {"description": "navy top, beige trousers", "bbox": [300, 10, 360, 200]},
                {"description": "no box", "bbox": None},
            ]
        }
    )
    rig.memory.locate_in_observation.side_effect = [
        {"position": [3.0, 1.0, 0.9], "method": "m", "point_count": 1},
        None,
    ]

    result = json.loads(rig.module.describe_visible_people())

    assert result["people"] == [
        {"description": "white t-shirt, dark trousers", "position": [3.0, 1.0]},
        {"description": "navy top, beige trousers", "position": None},
    ]


def test_navigate_to_person_stops_half_a_metre_short_facing_them(make_rig) -> None:  # type: ignore[no-untyped-def]
    rig = make_rig()
    rig.memory.locate_in_observation.return_value = {
        "position": [4.0, 0.0, 0.9],
        "method": "m",
        "point_count": 9,
    }

    result = rig.module.navigate_to_person(WHITE)

    (goal,) = rig.navigation.set_goal.call_args.args
    assert (goal.x, goal.y) == pytest.approx((3.5, 0.0))
    assert goal.orientation.euler.z == pytest.approx(0.0)
    assert "Started navigating" in result
    assert [tag.name for tag in rig.tags] == [f"person: {WHITE}"]


def test_navigate_to_person_does_nothing_when_already_close(make_rig) -> None:  # type: ignore[no-untyped-def]
    rig = make_rig()
    rig.memory.locate_in_observation.return_value = {
        "position": [0.6, 0.0, 0.9],
        "method": "m",
        "point_count": 9,
    }

    result = rig.module.navigate_to_person(WHITE)

    rig.navigation.set_goal.assert_not_called()
    assert "Already within" in result


def test_navigate_to_person_falls_back_to_the_last_seen_tag(make_rig) -> None:  # type: ignore[no-untyped-def]
    rig = make_rig()
    rig.module.tag_person(WHITE)
    rig.tags[0].timestamp = time.time() - 30
    rig.vlm.query.return_value = _person_json(None)

    result = rig.module.navigate_to_person(WHITE)

    assert "last seen 30 seconds ago" in result
    rig.navigation.set_goal.assert_called_once()


def test_navigate_to_person_without_sighting_or_tag_says_so(make_rig) -> None:  # type: ignore[no-untyped-def]
    rig = make_rig()
    rig.vlm.query.return_value = _person_json(None)

    assert "No person matching" in rig.module.navigate_to_person(WHITE)
    rig.navigation.set_goal.assert_not_called()


def test_navigation_refusal_is_reported(make_rig) -> None:  # type: ignore[no-untyped-def]
    rig = make_rig()
    rig.navigation.set_goal.return_value = False

    assert "refused" in rig.module.navigate_to_person(WHITE)


def _sighting(rig: Rig) -> _Sighting:
    image, _ = rig.memory.capture_object_observation.return_value
    return _Sighting((3.0, 1.0, 0.9), [100, 50, 200, 300], image, WHITE)


def _stop_after(stop: Event, looks: int, mocker) -> None:  # type: ignore[no-untyped-def]
    """Make the loop's wait() let `looks` iterations run, then report stop."""
    waits = 0

    def wait(_timeout: float) -> bool:
        nonlocal waits
        waits += 1
        return waits > looks or stop.is_set()

    mocker.patch.object(stop, "wait", side_effect=wait)


def _drive_follow_loop(rig: Rig, sightings: list[bool], mocker) -> None:  # type: ignore[no-untyped-def]
    """Run the follow loop once per entry: True = person found, False = not found."""
    stop = Event()
    _stop_after(stop, len(sightings), mocker)
    results = iter([_sighting(rig) if found else None for found in sightings])
    mocker.patch.object(rig.module, "_find_person", side_effect=lambda _d: next(results))
    rig.module._follow_loop(WHITE, stop)


def test_follow_loop_feeds_each_sighting_to_the_goal_tracker(make_rig, mocker) -> None:  # type: ignore[no-untyped-def]
    rig = make_rig(max_missed_looks=2)

    _drive_follow_loop(rig, [True, True], mocker)

    assert rig.tracker.update_target.call_count == 2
    rig.tracker.update_target.assert_called_with(3.0, 1.0)
    rig.tracker.stop_tracking.assert_called_once()
    rig.module.stop_tool.assert_called_once_with("follow_person_with_planner")


def test_follow_loop_gives_up_after_consecutive_misses(make_rig, mocker) -> None:  # type: ignore[no-untyped-def]
    rig = make_rig(max_missed_looks=3)

    _drive_follow_loop(rig, [True, False, False, False, True], mocker)

    assert rig.tracker.update_target.call_count == 1
    update = rig.module.tool_update.call_args.args[1]
    assert "lost sight" in update
    rig.tracker.stop_tracking.assert_called_once()


def test_a_found_person_resets_the_miss_count(make_rig, mocker) -> None:  # type: ignore[no-untyped-def]
    rig = make_rig(max_missed_looks=2)

    _drive_follow_loop(rig, [False, True, False, True], mocker)

    assert rig.tracker.update_target.call_count == 2
    assert "lost sight" not in rig.module.tool_update.call_args.args[1]


def test_follow_loop_survives_a_failing_lookup(make_rig, mocker) -> None:  # type: ignore[no-untyped-def]
    rig = make_rig(max_missed_looks=5)
    stop = Event()
    _stop_after(stop, 2, mocker)
    mocker.patch.object(
        rig.module, "_find_person", side_effect=[RuntimeError("camera"), _sighting(rig)]
    )

    rig.module._follow_loop(WHITE, stop)

    rig.tracker.update_target.assert_called_once_with(3.0, 1.0)


def test_follow_skill_starts_tracking_and_stop_skill_ends_it(make_rig, mocker) -> None:  # type: ignore[no-untyped-def]
    rig = make_rig()
    started = Event()

    def idle_loop(_description: str, stop_event: Event) -> None:
        started.set()
        stop_event.wait()

    mocker.patch.object(rig.module, "_follow_loop", side_effect=idle_loop)

    result = rig.module.follow_person_with_planner(WHITE)

    assert started.wait(5)
    assert "Following" in result
    rig.tracker.start_tracking.assert_called_once()
    rig.tracker.update_target.assert_called_once_with(3.0, 1.0)
    rig.module.stop_tool.assert_not_called()

    assert rig.module.stop_following_person() == "Stopped following."

    rig.tracker.stop_tracking.assert_called_once()


def test_follow_skill_releases_movement_when_the_person_is_not_found(make_rig) -> None:  # type: ignore[no-untyped-def]
    rig = make_rig()
    rig.vlm.query.return_value = _person_json(None)

    result = rig.module.follow_person_with_planner(WHITE)

    assert "No person matching" in result
    rig.tracker.start_tracking.assert_not_called()
    rig.module.stop_tool.assert_called_once_with("follow_person_with_planner")
