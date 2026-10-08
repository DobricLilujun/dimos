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

"""Keep the A* planner's goal on a moving target.

``GoalTracker`` watches a stream of target poses and, whenever the target has
moved at least ``update_threshold_m`` since the last goal it sent, publishes a
new ``goal_request``. The planner then replans from the robot's current pose.
The target is tracked as-is; its future position is not predicted.
"""

import math
from threading import RLock
import time
from typing import Any

from pydantic import Field
from reactivex.disposable import Disposable

from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.core.stream import In, Out
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Quaternion import Quaternion
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.navigation.go2.replanning_a_star.module_spec import ReplanningAStarPlannerSpec
from dimos.utils.logging_config import setup_logger

logger = setup_logger()

# Matches GlobalPlanner._goal_tolerance: inside this band the planner would
# call the goal reached anyway, so sending a new one only makes the robot wiggle.
_HOLD_MARGIN_M = 0.2


class GoalUpdatePolicy:
    """Decides when a new target position is worth a new navigation goal."""

    def __init__(
        self, update_threshold_m: float, min_update_interval_s: float, follow_distance_m: float
    ) -> None:
        self._update_threshold_m = update_threshold_m
        self._min_update_interval_s = min_update_interval_s
        self._follow_distance_m = follow_distance_m
        self._last_target: tuple[float, float] | None = None
        self._last_sent: float | None = None

    def reset(self) -> None:
        self._last_target = None
        self._last_sent = None

    def next_goal(self, target: Vector3, robot: Vector3, now: float) -> PoseStamped | None:
        """Return a goal for ``target``, or None if the current goal is still good.

        The target is compared with the last target a goal was sent for, not with
        the previous message, so slow drift adds up and eventually triggers an
        update. The robot's own motion never does.
        """
        if not all(
            math.isfinite(value) for value in (target.x, target.y, target.z, robot.x, robot.y)
        ):
            return None

        if self._last_sent is not None and now - self._last_sent < self._min_update_interval_s:
            return None

        if (
            self._last_target is not None
            and math.hypot(target.x - self._last_target[0], target.y - self._last_target[1])
            < self._update_threshold_m
        ):
            return None

        dx = target.x - robot.x
        dy = target.y - robot.y
        distance = math.hypot(dx, dy)
        if distance <= self._follow_distance_m + _HOLD_MARGIN_M:
            return None

        # Stop follow_distance_m short of the target, facing it.
        scale = (distance - self._follow_distance_m) / distance
        self._last_target = (target.x, target.y)
        self._last_sent = now
        return PoseStamped(
            ts=time.time(),
            frame_id="world",
            position=Vector3(robot.x + dx * scale, robot.y + dy * scale, target.z),
            orientation=Quaternion.from_euler(Vector3(0.0, 0.0, math.atan2(dy, dx))),
        )


class GoalTrackerConfig(ModuleConfig):
    # Inert unless a blueprint (or start_tracking) turns it on.
    enabled: bool = False
    # Send a new goal once the target has moved this far from the last goal's target.
    update_threshold_m: float = Field(default=0.5, gt=0.0, allow_inf_nan=False)
    # Lower bound on the time between two goals; planning runs on the stream thread.
    min_update_interval_s: float = Field(default=0.5, ge=0.0, allow_inf_nan=False)
    # Stop this far short of the target. 0 drives onto the target's position.
    follow_distance_m: float = Field(default=0.5, ge=0.0, allow_inf_nan=False)


class GoalTracker(Module):
    """Turns a moving target into a series of ``goal_request`` messages.

    ``tracked_target`` must be in the same frame as ``odom``; the planner does
    not look at ``frame_id``.
    """

    config: GoalTrackerConfig

    tracked_target: In[PoseStamped]
    odom: In[PoseStamped]

    goal_request: Out[PoseStamped]

    _planner: ReplanningAStarPlannerSpec | None = None

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._lock = RLock()
        self._active = self.config.enabled
        self._latest_odom: PoseStamped | None = None
        self._policy = GoalUpdatePolicy(
            self.config.update_threshold_m,
            self.config.min_update_interval_s,
            self.config.follow_distance_m,
        )

    @rpc
    def start(self) -> None:
        super().start()
        self.register_disposable(Disposable(self.odom.subscribe(self._on_odom)))
        self.register_disposable(Disposable(self.tracked_target.subscribe(self._on_target)))

    @rpc
    def stop(self) -> None:
        super().stop()

    @rpc
    def start_tracking(self) -> bool:
        """Start sending goals; the next target always produces one."""
        with self._lock:
            self._policy.reset()
            self._active = True
        return True

    @rpc
    def stop_tracking(self) -> bool:
        """Stop sending goals and cancel the planner's current one."""
        with self._lock:
            self._active = False
        if self._planner is not None:
            self._planner.cancel_goal()
        return True

    @rpc
    def update_target(self, x: float, y: float) -> bool:
        """Feed a target position by hand, e.g. from ``dimos shell``."""
        self._on_target(PoseStamped(frame_id="world", position=Vector3(x, y, 0.0)))
        return True

    def _on_odom(self, odom: PoseStamped) -> None:
        with self._lock:
            self._latest_odom = odom

    def _on_target(self, target: PoseStamped) -> None:
        with self._lock:
            if not self._active or self._latest_odom is None:
                return
            goal = self._policy.next_goal(
                target.position, self._latest_odom.position, time.monotonic()
            )

        if goal is None:
            return

        logger.info(
            "Tracked target moved. Updating goal.",
            target_x=round(target.x, 2),
            target_y=round(target.y, 2),
            goal_x=round(goal.x, 2),
            goal_y=round(goal.y, 2),
        )
        self.goal_request.publish(goal)
