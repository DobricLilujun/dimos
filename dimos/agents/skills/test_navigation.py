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

import json
from typing import Any

from langchain_core.messages import HumanMessage
import numpy as np
import pytest

from dimos.agents.skills.navigation import NavigationSkillContainer
from dimos.core.core import rpc
from dimos.core.module import Module
from dimos.core.stream import Out
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.sensor_msgs.Image import Image
from dimos.navigation.base import NavigationState
from dimos.types.robot_location import RobotLocation


class FakeCamera(Module):
    color_image: Out[Image]


class FakeOdom(Module):
    odom: Out[PoseStamped]


class StubSpatialMemory(Module):
    @rpc
    def capture_object_observation(self) -> tuple[Image, dict[str, Any]]:
        raise RuntimeError("No test observation")

    @rpc
    def tag_object_from_observation(
        self, name: str, bbox: list[int], image: Image, context: dict[str, Any]
    ) -> str:
        raise RuntimeError("No test observation")

    @rpc
    def get_robot_locations(self) -> list[RobotLocation]:
        return []

    @rpc
    def tag_location(self, robot_location: RobotLocation) -> bool:
        return True

    @rpc
    def query_tagged_location(self, query: str) -> RobotLocation | None:
        return None

    @rpc
    def query_by_text(self, text: str, limit: int = 5) -> list[dict[str, Any]]:
        return []


class StubNavigation(Module):
    @rpc
    def set_goal(self, goal: PoseStamped) -> bool:
        return True

    @rpc
    def get_state(self) -> NavigationState:
        return NavigationState.IDLE

    @rpc
    def is_goal_reached(self) -> bool:
        return False

    @rpc
    def cancel_goal(self) -> bool:
        return True


class StubObjectTracking(Module):
    @rpc
    def track(self, bbox: list[float]) -> dict[str, Any]:
        return {}

    @rpc
    def stop_track(self) -> bool:
        return True

    @rpc
    def is_tracking(self) -> bool:
        return False


_STUB_BLUEPRINTS = [
    StubSpatialMemory.blueprint(),
    StubNavigation.blueprint(),
    StubObjectTracking.blueprint(),
]


class MockedStopNavSkill(NavigationSkillContainer):
    _skill_started = True

    def _cancel_goal_and_stop(self):
        pass


class MockedExploreNavSkill(NavigationSkillContainer):
    _skill_started = True

    def _start_exploration(self, timeout):
        return "Exploration completed successfuly"

    def _cancel_goal_and_stop(self):
        pass


class MockedSemanticNavSkill(NavigationSkillContainer):
    _skill_started = True

    def _navigate_by_tagged_location(self, query):
        return None

    def _navigate_to_object(self, query):
        return None

    def _navigate_using_semantic_map(self, query):
        return f"Successfuly arrived at '{query}'"


def test_stop_movement(agent_setup) -> None:
    history = agent_setup(
        blueprints=[
            FakeCamera.blueprint(),
            FakeOdom.blueprint(),
            MockedStopNavSkill.blueprint(),
            *_STUB_BLUEPRINTS,
        ],
        messages=[HumanMessage("Stop moving. Use the stop_movement tool.")],
    )

    assert "stopped" in history[-1].content.lower()


def test_start_exploration(agent_setup) -> None:
    history = agent_setup(
        blueprints=[
            FakeCamera.blueprint(),
            FakeOdom.blueprint(),
            MockedExploreNavSkill.blueprint(),
            *_STUB_BLUEPRINTS,
        ],
        messages=[
            HumanMessage("Take a look around for 10 seconds. Use the start_exploration tool.")
        ],
    )

    assert "explor" in history[-1].content.lower()


def test_go_to_semantic_location(agent_setup) -> None:
    history = agent_setup(
        blueprints=[
            FakeCamera.blueprint(),
            FakeOdom.blueprint(),
            MockedSemanticNavSkill.blueprint(),
            *_STUB_BLUEPRINTS,
        ],
        messages=[HumanMessage("Go to the bookshelf. Use the navigate_with_text tool.")],
    )

    assert "success" in history[-1].content.lower()


@pytest.mark.parametrize("accepted", [False, True])
def test_navigation_reports_rejected_goals(mocker, accepted) -> None:
    mocker.patch("dimos.models.vl.qwen.QwenVlModel")
    container = NavigationSkillContainer()
    try:
        navigator = mocker.patch.object(container, "_navigation", create=True)
        navigator.set_goal.return_value = accepted
        result = container._navigate_to(PoseStamped(position=[1, 0, 0]), "Found office")
        assert ("Started navigating" in result) == accepted
        assert ("Navigation refused" in result) == (not accepted)
    finally:
        container.dispose()


@pytest.fixture
def memory_skills(mocker):
    mocker.patch("dimos.models.vl.qwen.QwenVlModel")
    container = NavigationSkillContainer()
    mocker.patch.object(container, "_skill_started", True)
    memory = mocker.patch.object(container, "_spatial_memory", create=True)
    navigator = mocker.patch.object(container, "_navigation", create=True)
    navigator.set_goal.return_value = True
    memory.get_robot_locations.return_value = [
        RobotLocation(
            name="fire extinguisher",
            position=(1, 2, 1.2),
            rotation=(0, 0, 0),
            location_id="ext-1",
            metadata={"kind": "object", "description": "Near door"},
        ),
        RobotLocation(
            name="Fire Extinguisher",
            position=(5, 6, 1.2),
            rotation=(0, 0, 0),
            location_id="ext-2",
        ),
        RobotLocation(name="office", position=(0, 0, 0), rotation=(0, 0, 0)),
    ]
    yield container, memory, navigator
    container.dispose()


def test_memory_inventory_lists_all_same_name_tags_without_movement(memory_skills):
    container, _, navigator = memory_skills
    result = json.loads(container.query_memory_tags("fire extinguisher"))
    assert result["matching_tag_count"] == 2
    assert result["total_stored_tags"] == 3
    assert [tag["id"] for tag in result["tags"]] == ["ext-1", "ext-2"]
    assert result["tags"][0]["position"] == [1, 2, 1.2]
    assert result["tags"][0]["kind"] == "object"
    assert result["tags"][0]["description"] == "Near door"
    assert result["tags"][1]["kind"] == "unknown"
    assert json.loads(container.query_memory_tags())["matching_tag_count"] == 3
    assert json.loads(container.query_memory_tags("missing"))["matching_tag_count"] == 0
    navigator.set_goal.assert_not_called()


@pytest.mark.parametrize("accepted", [True, False])
def test_memory_tag_navigation_uses_selected_id_and_respects_gate(memory_skills, accepted):
    container, _, navigator = memory_skills
    container._on_odom(PoseStamped(position=[0, 0, 0.3], frame_id="world"))
    navigator.set_goal.return_value = accepted
    result = container.navigate_to_memory_tag("ext-2")
    goal = navigator.set_goal.call_args.args[0]
    assert goal.position.to_numpy().tolist() == [5, 6, 0.3]
    assert goal.frame_id == "world"
    assert ("Started navigating" in result) == accepted
    assert ("Navigation refused" in result) != accepted


def test_memory_tag_navigation_rejects_unknown_id(memory_skills):
    container, _, navigator = memory_skills
    with pytest.raises(ValueError, match="No saved tag"):
        container.navigate_to_memory_tag("missing")
    navigator.set_goal.assert_not_called()


def test_memory_tools_are_exposed_with_typed_mcp_schemas(memory_skills):
    container, _, _ = memory_skills
    skills = {entry.func_name: entry for entry in container.get_skills()}
    query_schema = json.loads(skills["query_memory_tags"].args_schema)
    navigation_schema = json.loads(skills["navigate_to_memory_tag"].args_schema)
    assert query_schema["properties"]["query"]["type"] == "string"
    assert query_schema["properties"]["query"]["default"] == ""
    assert navigation_schema["required"] == ["location_id"]
    assert skills["query_memory_tags"].uses == ()
    assert "movement" in skills["navigate_to_memory_tag"].uses


def test_memory_tag_navigation_rejects_invalid_coordinates(memory_skills):
    container, memory, navigator = memory_skills
    memory.get_robot_locations.return_value = [
        RobotLocation("bad", (float("nan"), 0, 0), (0, 0, 0), location_id="bad")
    ]
    with pytest.raises(ValueError, match="invalid coordinates"):
        container.navigate_to_memory_tag("bad")
    navigator.set_goal.assert_not_called()


def test_object_tag_uses_captured_image_geometry_not_robot_pose(memory_skills, mocker):
    container, memory, navigator = memory_skills
    image = Image.from_numpy(np.zeros((100, 100, 3), dtype=np.uint8), ts=10.0)
    context = {"snapshot": "geometry"}
    memory.capture_object_observation.return_value = image, context
    detect = mocker.patch(
        "dimos.agents.skills.navigation.get_object_bbox_from_image",
        return_value=(40, 40, 60, 60),
    )
    memory.tag_object_from_observation.return_value = '{"position":[14,20,1]}'
    container._on_odom(PoseStamped(position=[100, 200, 0.3]))
    assert container.tag_object("fire extinguisher") == '{"position":[14,20,1]}'
    detect.assert_called_once_with(container._vl_model, image, "fire extinguisher")
    memory.tag_object_from_observation.assert_called_once_with(
        "fire extinguisher", [40, 40, 60, 60], image, context
    )
    memory.tag_location.assert_not_called()
    navigator.set_goal.assert_not_called()


def test_object_tag_does_not_save_when_detection_fails(memory_skills, mocker):
    container, memory, _ = memory_skills
    memory.capture_object_observation.return_value = (
        Image.from_numpy(np.zeros((100, 100, 3), dtype=np.uint8)),
        {},
    )
    mocker.patch("dimos.agents.skills.navigation.get_object_bbox_from_image", return_value=None)
    with pytest.raises(RuntimeError, match="No visible object"):
        container.tag_object("fire extinguisher")
    memory.tag_object_from_observation.assert_not_called()
