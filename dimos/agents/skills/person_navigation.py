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

"""Tag, navigate to and follow a person picked out by their appearance.

A person is found by asking a vision-language model for the box of whoever
matches a description such as "white t-shirt", then projecting that box to a
world position through the lidar snapshot taken with the same camera frame
(``SpatialMemory``). Following repeats this about every ``follow_interval_s``
and hands the position to ``GoalTracker``, which replans whenever the person has
moved far enough. The person is re-identified by appearance on every cycle, so
no tracker state can drift onto someone else.
"""

import json
import math
from threading import Event, RLock, Thread
import time
from typing import Any, NamedTuple

from pydantic import Field
from reactivex.disposable import Disposable

from dimos.agents.annotation import skill
from dimos.agents.capabilities import CAP_MOVEMENT
from dimos.constants import DEFAULT_THREAD_JOIN_TIMEOUT
from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.core.stream import In
from dimos.models.vl.base import VlModel
from dimos.models.vl.openai import OpenAIVlModel
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.sensor_msgs.Image import Image
from dimos.navigation.go2.replanning_a_star.goal_tracker import GoalUpdatePolicy
from dimos.navigation.go2.replanning_a_star.goal_tracker_spec import GoalTrackerSpec
from dimos.navigation.go2.replanning_a_star.spec import NavigationInterfaceSpec
from dimos.perception.experimental.spatial_memory_spec import PersonMemorySpec
from dimos.types.robot_location import RobotLocation
from dimos.utils.generic import extract_json_from_llm_response
from dimos.utils.logging_config import setup_logger

logger = setup_logger()

PERSON_TAG_KIND = "person"
_FOLLOW_TOOL = "follow_person_with_planner"

_FIND_PERSON_PROMPT = (
    "Look at this image and find the one person who matches this description: "
    "'{description}'. Judge by clothing and appearance, and never return a person who does "
    'not match. Return ONLY JSON of the form {{"bbox": [x1, y1, x2, y2]}} with integer pixel '
    "coordinates of the whole person (x1,y1 top-left, x2,y2 bottom-right), or "
    '{{"bbox": null}} if no visible person matches.'
)

_LIST_PEOPLE_PROMPT = (
    "List every person visible in this image. For each, give a short description of what they "
    "wear (for example 'white t-shirt, dark trousers') and the pixel bounding box of the whole "
    "person. Return ONLY JSON of the form "
    '{"people": [{"description": "...", "bbox": [x1, y1, x2, y2]}]}. '
    'Return {"people": []} if nobody is visible.'
)


# Appended to the robot's system prompt by blueprints that include this container.
PERSON_NAVIGATION_PROMPT = """

## People
- To go to or follow a person, identify them by what they wear. If several people could match, or you are unsure who is in view, call `describe_visible_people` first and use a description that tells them apart.
- `follow_person_with_planner(description)` follows one person and re-plans around obstacles as they move. It ignores other people. Stop it with `stop_following_person`.
- `navigate_to_person(description)` walks to where a person is right now, once. `tag_person(description)` remembers where they were seen; people move, so a person tag is only a last-seen position.
- If a person cannot be found, say so and ask what to do, or turn and look again. Never invent a position for a person.
"""


class PersonNavigationConfig(ModuleConfig):
    vlm_model: str = "gpt-5.6-luna"
    # An OpenAI-compatible endpoint; None means the official OpenAI API.
    vlm_url: str | None = None
    # Time between looks while following. A look takes as long as the model call.
    follow_interval_s: float = Field(default=1.5, gt=0.0, allow_inf_nan=False)
    # Consecutive looks without finding the person before following gives up.
    max_missed_looks: int = Field(default=4, ge=1)
    # Distance at which navigate_to_person stops short of the person.
    approach_distance_m: float = Field(default=0.5, ge=0.0, allow_inf_nan=False)
    # Keep the person's tag at their latest seen position while following.
    refresh_tag_while_following: bool = True


class _Sighting(NamedTuple):
    position: tuple[float, float, float]
    bbox: list[int]
    image: Image
    description: str


class _Miss(NamedTuple):
    """No usable sighting; ``seen_without_depth`` means the VLM found them but lidar did not."""

    seen_without_depth: bool


class PersonNavigationSkillContainer(Module):
    config: PersonNavigationConfig

    odom: In[PoseStamped]

    _spatial_memory: PersonMemorySpec
    _navigation: NavigationInterfaceSpec
    _goal_tracker: GoalTrackerSpec

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        url = self.config.vlm_url.rstrip("/") if self.config.vlm_url else None
        self._vl_model: VlModel = OpenAIVlModel(
            model_name=self.config.vlm_model,
            base_url=(url if url is None or url.endswith("/v1") else f"{url}/v1"),
            api_key=self.config.g.openai_api_key,
        )
        self._lock = RLock()
        self._latest_odom: PoseStamped | None = None
        self._stop_following = Event()
        self._follow_thread: Thread | None = None

    @rpc
    def start(self) -> None:
        super().start()
        self.register_disposable(Disposable(self.odom.subscribe(self._on_odom)))

    @rpc
    def stop(self) -> None:
        self._halt_following()
        super().stop()

    def _on_odom(self, odom: PoseStamped) -> None:
        with self._lock:
            self._latest_odom = odom

    @skill
    def describe_visible_people(self) -> str:
        """List the people in the camera view, how they are dressed, and where they are.

        Use this to tell people apart before tagging, navigating to or following one, and
        to pick a description that identifies the right person. Positions are world
        coordinates (null if there is no valid lidar depth). It does not move the robot.
        """
        image, context = self._spatial_memory.capture_object_observation()
        response = self._vl_model.query(image, _LIST_PEOPLE_PROMPT)
        parsed = extract_json_from_llm_response(response)
        entries = parsed.get("people", []) if isinstance(parsed, dict) else []
        people = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            bbox = _parse_bbox(entry.get("bbox"))
            if bbox is None:
                continue
            description = str(entry.get("description", "person")).strip() or "person"
            estimate = self._spatial_memory.locate_in_observation(
                f"person {description}", bbox, image, context
            )
            people.append(
                {
                    "description": description,
                    "position": estimate["position"][:2] if estimate else None,
                }
            )
        return json.dumps({"frame": "world", "people": people}, ensure_ascii=False)

    @skill
    def tag_person(self, description: str) -> str:
        """Remember where a visible person is, identified by what they wear.

        Finds the person matching the description in the camera view, locates them
        with lidar, and saves a tag named 'person: <description>' with a photo of them.
        Tagging the same description again moves that tag instead of adding a new one.
        A person moves, so the tag is where they were last seen, not where they are now.
        Does not move the robot.

        Args:
            description: How to recognise the person, e.g. "white t-shirt".
        """
        sighting = self._find_person(description)
        if isinstance(sighting, _Miss):
            return self._not_found_message(description, sighting)
        tag_id = self._remember(sighting)
        x, y, _ = sighting.position
        return (
            f"Tagged person '{description}' (tag {tag_id}) at world ({x:.2f}, {y:.2f}). "
            "This is where they were seen just now; they may move."
        )

    @skill(uses=[CAP_MOVEMENT])
    def navigate_to_person(self, description: str) -> str:
        """Walk up to a person identified by what they wear, stopping about half a metre away.

        Looks for the person in the camera view first. If they are not visible it goes to
        where a saved person tag says they were last seen and tells you how old that is.
        Starts navigation and returns; it does not wait for arrival and does not keep
        following if the person moves. Use follow_person_with_planner for that.

        Args:
            description: How to recognise the person, e.g. "white t-shirt".
        """
        sighting = self._find_person(description)
        if not isinstance(sighting, _Miss):
            self._remember(sighting)
            x, y, z = sighting.position
            note = "Seen just now"
        else:
            tag = self._latest_person_tag(description)
            if tag is None:
                return self._not_found_message(description, sighting)
            x, y, z = tag.position
            note = (
                f"Not visible; going to where they were last seen {time.time() - tag.timestamp:.0f}"
                " seconds ago, they may have moved"
            )
        goal = self._approach_goal(Vector3(x, y, z))
        if goal is None:
            return f"{note}. Already within {self.config.approach_distance_m:.1f} m of them."
        if not self._navigation.set_goal(goal):
            return "Navigation refused. Check navigation readiness before retrying."
        return (
            f"{note} at world ({x:.2f}, {y:.2f}). Started navigating to them. "
            "To cancel movement call the 'stop_navigation' tool."
        )

    @skill(uses=[CAP_MOVEMENT], lifecycle="background")
    def follow_person_with_planner(self, description: str) -> str:
        """Follow a person identified by what they wear, planning a path around obstacles.

        Finds the person, then keeps looking for them about every second and a half and
        re-plans whenever they have moved about half a metre, stopping close behind them.
        It re-identifies them by appearance each time, so it ignores other people. Gives up
        after several looks without seeing them. Stop it with stop_following_person.

        Args:
            description: How to recognise the person, e.g. "white t-shirt".
        """
        self._halt_following()
        # Opened up front so every early return releases the movement capability via
        # stop_tool; only a started follow loop keeps the stream open.
        self.start_tool(_FOLLOW_TOOL)
        launched = False
        try:
            sighting = self._find_person(description)
            if isinstance(sighting, _Miss):
                return self._not_found_message(description, sighting)
            self._goal_tracker.start_tracking()
            self._goal_tracker.update_target(sighting.position[0], sighting.position[1])
            if self.config.refresh_tag_while_following:
                self._remember(sighting)
            stop_event = Event()
            with self._lock:
                self._stop_following = stop_event
                self._follow_thread = Thread(
                    target=self._follow_loop,
                    args=(description, stop_event),
                    name="PersonFollowLoop",
                    daemon=True,
                )
                self._follow_thread.start()
            launched = True
            return (
                f"Found the person '{description}'. Following them; I will re-plan as they move. "
                "Call 'stop_following_person' to stop. You will receive streaming updates."
            )
        finally:
            if not launched:
                self.stop_tool(_FOLLOW_TOOL)

    @skill
    def stop_following_person(self) -> str:
        """Stop following the person and cancel the current navigation goal."""
        self._halt_following()
        self._goal_tracker.stop_tracking()
        return "Stopped following."

    def _follow_loop(self, description: str, stop_event: Event) -> None:
        missed = 0
        reason = "it was requested to stop following"
        while not stop_event.wait(self.config.follow_interval_s):
            try:
                sighting = self._find_person(description)
            except Exception:
                logger.exception("Person lookup failed while following", description=description)
                sighting = _Miss(False)
            if stop_event.is_set():
                break
            if isinstance(sighting, _Miss):
                missed += 1
                if missed >= self.config.max_missed_looks:
                    reason = f"it lost sight of the person '{description}'"
                    break
                continue
            missed = 0
            self._goal_tracker.update_target(sighting.position[0], sighting.position[1])
            if self.config.refresh_tag_while_following:
                self._remember(sighting)
        self._goal_tracker.stop_tracking()
        self.tool_update(_FOLLOW_TOOL, f"Person follow stopped. Reason: {reason}.")
        self.stop_tool(_FOLLOW_TOOL)
        logger.info("Person follow stopped", description=description, reason=reason)

    def _halt_following(self) -> None:
        with self._lock:
            thread = self._follow_thread
            self._follow_thread = None
            stop_event = self._stop_following
        stop_event.set()
        if thread is not None:
            thread.join(DEFAULT_THREAD_JOIN_TIMEOUT)

    def _find_person(self, description: str) -> _Sighting | _Miss:
        image, context = self._spatial_memory.capture_object_observation()
        response = self._vl_model.query(image, _FIND_PERSON_PROMPT.format(description=description))
        parsed = extract_json_from_llm_response(response)
        bbox = _parse_bbox(parsed.get("bbox") if isinstance(parsed, dict) else None)
        if bbox is None:
            return _Miss(seen_without_depth=False)
        estimate = self._spatial_memory.locate_in_observation(
            f"person {description}", bbox, image, context
        )
        if estimate is None:
            logger.warning("Person found but has no valid lidar depth", description=description)
            return _Miss(seen_without_depth=True)
        x, y, z = estimate["position"]
        return _Sighting((x, y, z), bbox, image, description)

    def _remember(self, sighting: _Sighting) -> str:
        """Create or move the person's tag; returns its id."""
        name = _tag_name(sighting.description)
        x, y, z = sighting.position
        existing = self._latest_person_tag(sighting.description, exact=True)
        if existing is not None:
            self._spatial_memory.update_robot_location(existing.location_id, [x, y, z])
            return existing.location_id
        x1, y1, x2, y2 = sighting.bbox
        frame = sighting.image.to_opencv()
        crop = frame[max(0, y1) : max(0, y2), max(0, x1) : max(0, x2)]
        self._spatial_memory.add_named_location(
            name,
            [x, y, z],
            [0.0, 0.0, 0.0],
            f"Person seen wearing: {sighting.description}",
            kind=PERSON_TAG_KIND,
            reference_image=crop.copy() if crop.size else None,
        )
        created = self._latest_person_tag(sighting.description, exact=True)
        return created.location_id if created is not None else "unknown"

    def _latest_person_tag(self, description: str, *, exact: bool = False) -> RobotLocation | None:
        wanted = _tag_name(description).casefold()
        matches = [
            location
            for location in self._spatial_memory.get_robot_locations()
            if location.metadata.get("kind") == PERSON_TAG_KIND
            and (
                location.name.casefold() == wanted
                if exact
                else description.casefold() in location.name.casefold()
            )
        ]
        return max(matches, key=lambda location: location.timestamp, default=None)

    def _approach_goal(self, person: Vector3) -> PoseStamped | None:
        with self._lock:
            odom = self._latest_odom
        if odom is None:
            raise RuntimeError("No odometry yet; cannot plan an approach")
        policy = GoalUpdatePolicy(
            update_threshold_m=1e-6,
            min_update_interval_s=0.0,
            follow_distance_m=self.config.approach_distance_m,
        )
        return policy.next_goal(person, odom.position, time.monotonic())

    @staticmethod
    def _not_found_message(description: str, miss: _Miss) -> str:
        if miss.seen_without_depth:
            return (
                f"A person matching '{description}' is visible, but too far away for the lidar "
                "to measure where they are. Move closer to them (move_to with relative=True, "
                "x forward) and try again."
            )
        return (
            f"No person matching '{description}' is visible. Call describe_visible_people to "
            "see who is in view, or turn the robot and retry."
        )


def _tag_name(description: str) -> str:
    return f"person: {description.strip()}"


def _parse_bbox(raw: Any) -> list[int] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        values = [float(value) for value in raw]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    x1, y1, x2, y2 = values
    if x2 <= x1 or y2 <= y1:
        return None
    return [round(value) for value in values]
