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

import copy
import json
import math
import time
from typing import Any

from reactivex.disposable import Disposable

from dimos.agents.annotation import skill
from dimos.agents.capabilities import CAP_MOVEMENT
from dimos.agents.skills.visual_servoing.query import get_object_bbox_from_image
from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.core.stream import In
from dimos.models.qwen.bbox import BBox
from dimos.models.vl.base import VlModel
from dimos.models.vl.openai import OpenAIVlModel
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Quaternion import Quaternion
from dimos.msgs.geometry_msgs.Vector3 import Vector3, make_vector3
from dimos.msgs.sensor_msgs.Image import Image
from dimos.navigation.base import NavigationState
from dimos.navigation.go2.loop_closure.memory_spec import NearbyNavigationSpec, TaggedNavigationSpec
from dimos.navigation.go2.replanning_a_star.spec import NavigationInterfaceSpec
from dimos.perception.experimental.object_tracking_spec import ObjectTrackingSpec
from dimos.perception.experimental.spatial_memory_spec import SpatialMemorySpec
from dimos.types.robot_location import RobotLocation
from dimos.utils.logging_config import setup_logger

logger = setup_logger()


class NavigationSkillContainerConfig(ModuleConfig):
    vlm_url: str | None = None
    vlm_model: str = "gpt-4o-mini"


class NavigationSkillContainer(Module):
    config: NavigationSkillContainerConfig
    _vl_model: VlModel
    _latest_image: Image | None = None
    _latest_odom: PoseStamped | None = None
    _skill_started: bool = False
    _similarity_threshold: float = 0.23

    _spatial_memory: SpatialMemorySpec
    _navigation: NavigationInterfaceSpec
    _object_tracking: ObjectTrackingSpec | None = None
    _tagged_navigation: TaggedNavigationSpec | None = None
    _nearby_navigation: NearbyNavigationSpec | None = None

    color_image: In[Image]
    odom: In[PoseStamped]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._skill_started = False
        self._starting_pose: PoseStamped | None = None

        if self.config.vlm_url:
            url = self.config.vlm_url.rstrip("/")
            self._vl_model = OpenAIVlModel(
                model_name=self.config.vlm_model,
                base_url=url if url.endswith("/v1") else f"{url}/v1",
                api_key=self.config.g.openai_api_key,
            )
        else:
            # Preserve the existing provider when no console VLM override is supplied.
            from dimos.models.vl.qwen import QwenVlModel

            self._vl_model = QwenVlModel()

    @rpc
    def start(self) -> None:
        super().start()
        self._starting_pose = None
        self.register_disposable(Disposable(self.color_image.subscribe(self._on_color_image)))
        self.register_disposable(Disposable(self.odom.subscribe(self._on_odom)))
        self._skill_started = True

    @rpc
    def stop(self) -> None:
        super().stop()

    def _on_color_image(self, image: Image) -> None:
        self._latest_image = image

    def _on_odom(self, odom: PoseStamped) -> None:
        self._latest_odom = odom
        if self._starting_pose is None and odom.frame_id == "world":
            if all(
                math.isfinite(value)
                for value in (odom.position.x, odom.position.y, odom.position.z)
            ):
                self._starting_pose = copy.deepcopy(odom)

    @skill
    def query_starting_location(self) -> str:
        """Query this run's starting location in world coordinates without moving.

        Captured from the first valid aligned world odometry; after reconnecting
        this is the new run's starting position, not a previous run's position.
        When asked to return to the starting location, use
        return_to_starting_location; do not assume the start is (0, 0).
        """
        if self._starting_pose is None:
            raise RuntimeError("Starting location unavailable: await aligned world odometry.")
        position = self._starting_pose.position
        return json.dumps(
            {
                "name": "starting location",
                "frame": "world",
                "position": [position.x, position.y, position.z],
                "timestamp": self._starting_pose.ts,
            }
        )

    @skill(uses=[CAP_MOVEMENT])
    def return_to_starting_location(self) -> str:
        """Navigate back to this run's starting location when explicitly requested.

        Requires captured aligned world odometry and the normal navigation
        readiness checks. This starts navigation; it does not mean arrival.
        """
        if self._starting_pose is None:
            raise RuntimeError("Starting location unavailable: await aligned world odometry.")
        goal = copy.deepcopy(self._starting_pose)
        goal.ts = time.time()
        return self._navigate_to(goal, "Returning to the starting location")

    @skill
    def query_memory_tags(self, query: str = "") -> str:
        """List persisted tags and world coordinates without moving the robot.

        Empty query lists all tags. Otherwise match tag names by case-insensitive
        substring (use names returned by this tool, not translated names).
        Returns every matching tag's ID, name, position, source category, and
        description. Count means stored tags, not verified physical objects.
        Old tags may have an unknown source. Object positions are estimates;
        location tags can be robot observation positions. Never use navigation
        tools merely to answer an inventory or coordinate question.
        """
        locations = self._spatial_memory.get_robot_locations()
        matches = [
            location
            for location in locations
            if query.strip().casefold() in location.name.casefold()
        ]
        return json.dumps(
            {
                "frame": "world",
                "query": query,
                "total_stored_tags": len(locations),
                "matching_tag_count": len(matches),
                "tags": [
                    {
                        "id": location.location_id,
                        "name": location.name,
                        "position": location.position,
                        "kind": location.metadata.get("kind", "unknown"),
                        "description": location.metadata.get("description", ""),
                    }
                    for location in matches
                ],
            },
            ensure_ascii=False,
        )

    @skill(uses=[CAP_MOVEMENT])
    def navigate_near_memory_tag(self, location_id: str) -> str:
        """Approach a saved tag using the configured horizontal arrival radius (default 1 m).

        Prefer this for conversational requests to go near an object or place.
        First query_memory_tags and choose a stable ID. If several match, ask
        which one. Does not require exact position or final orientation.
        Requires the persistent Go2 planner; old precise tools are unchanged.
        Starts navigation, not a claim of arrival. PGO refreshes the same tag ID.
        """
        if not self._skill_started:
            raise ValueError(f"{self} has not been started.")
        if self._nearby_navigation is None:
            raise RuntimeError("Nearby navigation requires the persistent Go2 planner")
        location = next(
            (
                item
                for item in self._spatial_memory.get_robot_locations()
                if item.location_id == location_id
            ),
            None,
        )
        if location is None:
            raise ValueError(f"No saved tag with ID {location_id!r}; query_memory_tags again")
        if not all(math.isfinite(value) for value in location.position):
            raise ValueError(f"Tag {location_id} has invalid coordinates")
        goal = PoseStamped(
            position=Vector3(
                location.position[0],
                location.position[1],
                self._latest_odom.position.z if self._latest_odom is not None else 0.0,
            ),
            frame_id="world",
        )
        if not self._nearby_navigation.set_nearby_tagged_goal(location_id, goal):
            return "Navigation refused. Check map alignment and PGO synchronization."
        return f"Approaching '{location.name}'; will stop within the configured nearby distance (default 1 meter). This is not confirmation of arrival."

    @skill(uses=[CAP_MOVEMENT])
    def navigate_to_memory_tag(self, location_id: str) -> str:
        """Navigate to a saved tag selected using query_memory_tags.

        Only call when the user requests movement. If multiple same-name tags
        exist, ask which one unless the user supplied a selection criterion.
        Uses the saved world coordinates and the existing planner, including
        its map-alignment gate. Object coordinates are estimates; the planner
        may choose a reachable nearby goal. Returning started is not arrival.
        """
        if not self._skill_started:
            raise ValueError(f"{self} has not been started.")
        for location in self._spatial_memory.get_robot_locations():
            if location.location_id == location_id:
                if not all(
                    math.isfinite(value) for value in (*location.position, *location.rotation)
                ):
                    raise ValueError(f"Tag {location_id} has invalid coordinates")
                pose = PoseStamped(
                    position=make_vector3(
                        location.position[0],
                        location.position[1],
                        self._latest_odom.position.z if self._latest_odom is not None else 0.0,
                    ),
                    orientation=Quaternion.from_euler(Vector3(*location.rotation)),
                    frame_id="world",
                )
                return self._navigate_to(
                    pose, f"Selected saved tag '{location.name}' ({location_id})", location_id
                )
        raise ValueError(f"No saved tag with ID {location_id!r}; query_memory_tags again")

    @skill
    def tag_object(self, object_name: str) -> str:
        """Locate and tag a visible object's actual estimated world position without moving.

        Detects its image bounding box, refines it with the configured segmenter,
        and uses timestamp-aligned camera TF, intrinsics, and lidar points to
        estimate the object surface in world coordinates. Requires a clear view
        and valid depth; never substitutes robot position or a default distance.
        Use this for objects such as fire extinguishers, not tag_location.
        """
        if not self._skill_started:
            raise ValueError(f"{self} has not been started.")
        image, context = self._spatial_memory.capture_object_observation()
        bbox = get_object_bbox_from_image(self._vl_model, image, object_name)
        if bbox is None:
            raise RuntimeError(f"No visible object matching '{object_name}'")
        return self._spatial_memory.tag_object_from_observation(
            object_name, [round(value) for value in bbox], image, context
        )

    @skill
    def tag_location(self, location_name: str) -> str:
        """Tag this location in the spatial memory with a name.

        Records the ROBOT's current position, not an object's position.
        For a fire extinguisher or another visible object, use tag_object instead.
        This associates the current location with the given name in the spatial memory, allowing you to navigate back to it.

        Args:
            location_name (str): the name for the location

        Returns:
            str: the outcome
        """

        if not self._skill_started:
            raise ValueError(f"{self} has not been started.")

        if not self._latest_odom:
            return "No odometry data received yet, cannot tag location."

        position = self._latest_odom.position
        rotation = self._latest_odom.orientation

        location = RobotLocation(
            name=location_name,
            position=(position.x, position.y, position.z),
            rotation=(rotation.x, rotation.y, rotation.z),
        )

        if not self._spatial_memory.tag_location(location):
            return f"Error: Failed to store '{location_name}' in the spatial memory"

        logger.info(f"Tagged {location}")
        return f"Tagged '{location_name}': ({position.x},{position.y})."

    # TODO(capabilities): this skill is `instant`, so the `movement` hold is
    # released the moment the call returns even though the tagged-location and
    # semantic-map paths only fire set_goal() and keep navigating. Make it
    # `background` and close the hold when the robot actually stops (the
    # planner already emits a goal-reached signal, see PatrollingModule) so
    # patrol/follow/explore can't start over an active navigation goal.
    @skill(uses=[CAP_MOVEMENT])
    def navigate_with_text(self, query: str) -> str:
        """Navigate to a location by querying the existing semantic map using natural language.

        First attempts to locate an object in the robot's camera view using vision.
        If the object is found, navigates to it. If not, falls back to querying the
        semantic map for a location matching the description.
        CALL THIS SKILL FOR ONE SUBJECT AT A TIME. For example: "Go to the person wearing a blue shirt in the living room",
        you should call this skill twice, once for the person wearing a blue shirt and once for the living room.
        Args:
            query: Text query to search for in the semantic map
        """

        if not self._skill_started:
            raise ValueError(f"{self} has not been started.")
        success_msg = self._navigate_by_tagged_location(query)
        if success_msg:
            return success_msg

        logger.info(f"No tagged location found for {query}")

        success_msg = self._navigate_to_object(query)
        if success_msg:
            return success_msg

        logger.info(f"No object in view found for {query}")

        success_msg = self._navigate_using_semantic_map(query)
        if success_msg:
            return success_msg

        return f"No tagged location called '{query}'. No object in view matching '{query}'. No matching location found in semantic map for '{query}'."

    def _navigate_by_tagged_location(self, query: str) -> str | None:
        robot_location = self._spatial_memory.query_tagged_location(query)

        if not robot_location:
            return None

        logger.info("Found tagged location", location=robot_location)
        goal_pose = PoseStamped(
            position=make_vector3(*robot_location.position),
            orientation=Quaternion.from_euler(Vector3(*robot_location.rotation)),
            frame_id="map",
        )

        return self._navigate_to(
            goal_pose, f"Found a tagged location called '{query}'.", robot_location.location_id
        )

    def _navigate_to(self, pose: PoseStamped, message: str, location_id: str | None = None) -> str:
        logger.info(
            f"Navigating to pose: ({pose.position.x:.2f}, {pose.position.y:.2f}, {pose.position.z:.2f})"
        )
        accepted = (
            self._tagged_navigation.set_tagged_goal(location_id, pose)
            if location_id is not None and self._tagged_navigation is not None
            else self._navigation.set_goal(pose)
        )
        if not accepted:
            return (
                "Navigation refused. Check map alignment and navigation readiness before retrying."
            )

        return (
            f"{message}. Started navigating to that position. "
            f"To cancel movement call the 'stop_navigation' tool."
        )

    def _navigate_to_object(self, query: str) -> str | None:
        if self._object_tracking is None:
            return None

        try:
            bbox = self._get_bbox_for_current_frame(query)
        except Exception:
            logger.error(f"Failed to get bbox for {query}", exc_info=True)
            return None

        if bbox is None:
            return None

        logger.info(f"Found {query} at {bbox}")

        # Start tracking - BBoxNavigationModule automatically generates goals
        self._object_tracking.track(bbox)  # type: ignore[arg-type]

        start_time = time.time()
        timeout = 30.0
        goal_set = False

        while time.time() - start_time < timeout:
            # Check if navigator finished
            if self._navigation.get_state() == NavigationState.IDLE and goal_set:
                logger.info("Waiting for goal result")
                time.sleep(1.0)
                if not self._navigation.is_goal_reached():
                    logger.info(f"Goal cancelled, tracking '{query}' failed")
                    self._object_tracking.stop_track()
                    return None
                else:
                    logger.info(f"Reached '{query}'")
                    self._object_tracking.stop_track()
                    return f"Successfully arrived at '{query}'"

            # If goal set and tracking lost, just continue (tracker will resume or timeout)
            if goal_set and not self._object_tracking.is_tracking():
                continue

            # BBoxNavigationModule automatically sends goals when tracker publishes
            # Just check if we have any detections to mark goal_set
            if self._object_tracking.is_tracking():
                goal_set = True

            time.sleep(0.25)

        logger.warning(f"Navigation to '{query}' timed out after {timeout}s")
        self._object_tracking.stop_track()
        return None

    def _get_bbox_for_current_frame(self, query: str) -> BBox | None:
        if self._latest_image is None:
            return None

        return get_object_bbox_from_image(self._vl_model, self._latest_image, query)

    def _navigate_using_semantic_map(self, query: str) -> str:
        results = self._spatial_memory.query_by_text(query)

        if not results:
            return f"No matching location found in semantic map for '{query}'"

        best_match = results[0]

        goal_pose = self._get_goal_pose_from_result(best_match)

        logger.info("Goal pose for semantic nav", pose=goal_pose)
        if not goal_pose:
            return f"Found a result for '{query}' but it didn't have a valid position."

        message = f"Found a location in the semantic map matching '{query}'."
        return self._navigate_to(goal_pose, message)

    @skill
    def stop_navigation(self) -> str:
        """Immediatly stop moving."""

        if not self._skill_started:
            raise ValueError(f"{self} has not been started.")

        self._cancel_goal_and_stop()

        return "Stopped"

    def _cancel_goal_and_stop(self) -> None:
        self._navigation.cancel_goal()

    def _get_goal_pose_from_result(self, result: dict[str, Any]) -> PoseStamped | None:
        similarity = 1.0 - (result.get("distance") or 1)
        if similarity < self._similarity_threshold:
            logger.warning(
                f"Match found but similarity score ({similarity:.4f}) is below threshold ({self._similarity_threshold})"
            )
            return None

        metadata = result.get("metadata")
        if not metadata:
            return None
        first = metadata[0]
        pos_x = first.get("pos_x", 0)
        pos_y = first.get("pos_y", 0)
        theta = first.get("rot_z", 0)

        return PoseStamped(
            position=make_vector3(pos_x, pos_y, 0),
            orientation=Quaternion.from_euler(make_vector3(0, 0, theta)),
            frame_id="map",
        )
