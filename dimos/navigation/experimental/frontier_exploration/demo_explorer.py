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

"""Demo exploration: reachable frontier goals and interruptible navigation feedback."""

from __future__ import annotations

import heapq
import math
import threading
import time
from typing import Any, Literal

from dimos_lcm.std_msgs import Bool, String
import numpy as np
from pydantic import BaseModel, Field
from scipy.ndimage import binary_dilation, label

from dimos.agents.annotation import skill
from dimos.agents.capabilities import CAP_MOVEMENT
from dimos.constants import DEFAULT_THREAD_JOIN_TIMEOUT
from dimos.core.core import rpc
from dimos.core.stream import Out
from dimos.mapping.occupancy.inflation import simple_inflate
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.nav_msgs.OccupancyGrid import CostValues, OccupancyGrid
from dimos.navigation.experimental.frontier_exploration.wavefront_frontier_goal_selector import (
    WavefrontConfig,
    WavefrontFrontierExplorer,
)
from dimos.navigation.go2.replanning_a_star.spec import NavigationInterfaceSpec
from dimos.utils.logging_config import setup_logger

logger = setup_logger()


class ExploreOptions(BaseModel):
    strategy: Literal["frontier", "efficient"] = "frontier"
    min_goals: int = Field(default=10, ge=0, le=1000)
    gain_percent: float = Field(default=1.0, ge=0, le=100, allow_inf_nan=False)
    no_gain_attempts: int = Field(default=2, ge=1, le=100)
    check_interval: float = Field(default=3.0, ge=0.1, le=60, allow_inf_nan=False)


class DemoExplorerConfig(WavefrontConfig):
    stall_timeout: float = Field(default=15.0, ge=3, le=300, allow_inf_nan=False)
    failure_cooldown: float = Field(default=60.0, ge=1, allow_inf_nan=False)
    sensor_timeout: float = Field(default=5.0, ge=1, allow_inf_nan=False)


def reachable_distances(grid: OccupancyGrid, pose: Vector3) -> np.ndarray[Any, Any]:
    """Dijkstra over free cells; diagonal steps cannot cut occupied corners."""
    free = grid.grid == CostValues.FREE
    distances = np.full(free.shape, np.inf)
    start = grid.world_to_grid(pose)
    x, y = int(start.x), int(start.y)
    if not (0 <= x < grid.width and 0 <= y < grid.height and free[y, x]):
        return distances
    distances[y, x] = 0
    queue = [(0.0, y, x)]
    while queue:
        distance, row, col = heapq.heappop(queue)
        if distance != distances[row, col]:
            continue
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
            ny, nx = row + dy, col + dx
            if not (0 <= nx < grid.width and 0 <= ny < grid.height and free[ny, nx]):
                continue
            if dx and dy and not (free[row, nx] and free[ny, col]):
                continue
            candidate = distance + grid.resolution * (math.sqrt(2) if dx and dy else 1)
            if candidate < distances[ny, nx]:
                distances[ny, nx] = candidate
                heapq.heappush(queue, (candidate, ny, nx))
    return distances


class DemoExplorer(WavefrontFrontierExplorer):
    config: DemoExplorerConfig
    _navigation: NavigationInterfaceSpec
    exploration_state: Out[String]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.options = ExploreOptions()
        self._guard = threading.RLock()
        self._wake = threading.Event()
        self._result: bool | None = None
        self._awaiting_goal = False
        self._failed: list[tuple[Vector3, float]] = []
        self._phase = "idle"
        self._reason = "Not started"
        self._odom_received = 0.0

    def _status(self, phase: str, reason: str) -> None:
        with self._guard:
            self._phase, self._reason = phase, reason
        logger.info("Demo exploration", phase=phase, reason=reason)
        self.exploration_state.publish(String(f"{phase}: {reason}"))

    @rpc
    def exploration_status(self) -> dict[str, Any]:
        with self._guard:
            return {
                "active": self.exploration_active,
                "phase": self._phase,
                "reason": self._reason,
                "reached_goals": len(self.explored_goals),
                **self.options.model_dump(),
            }

    def _on_costmap(self, msg: OccupancyGrid) -> None:
        self.latest_costmap = msg

    def _on_odometry(self, msg: PoseStamped) -> None:
        self.latest_odometry = msg
        self._odom_received = time.monotonic()

    def _on_goal_reached(self, msg: Bool) -> None:
        with self._guard:
            if self._awaiting_goal:
                self._result = bool(msg.data)
                self._wake.set()

    def _sensors_fresh(self) -> bool:
        return (
            self.latest_costmap is not None
            and self.latest_odometry is not None
            and time.monotonic() - self._odom_received <= self.config.sensor_timeout
        )

    def select_goal(self, pose: Vector3, grid: OccupancyGrid) -> Vector3 | None:
        distances = reachable_distances(grid, pose)
        frontier = np.isfinite(distances) & binary_dilation(
            grid.grid == CostValues.UNKNOWN, structure=np.ones((3, 3), dtype=bool)
        )
        self._failed = [(p, expiry) for p, expiry in self._failed if expiry > time.monotonic()]
        yy, xx = np.indices(frontier.shape)
        for previous in [*self.explored_goals, *(p for p, _ in self._failed), pose]:
            cell = grid.world_to_grid(previous)
            radius = (0.4 if previous is pose else 0.75) / grid.resolution
            frontier &= (xx - cell.x) ** 2 + (yy - cell.y) ** 2 >= radius**2
        clusters, count = label(frontier)
        goals: list[Vector3] = []
        sizes: list[int] = []
        costs: list[float] = []
        for cluster in range(1, count + 1):
            rows, cols = np.where(clusters == cluster)
            if len(rows) * grid.resolution < self.config.min_frontier_perimeter:
                continue
            # Closest reachable representative, not a centroid in unknown space.
            index = int(np.argmin(distances[rows, cols]))
            world = grid.grid_to_world((int(cols[index]), int(rows[index]), 0))
            goal = Vector3(world.x, world.y, 0)
            if math.hypot(goal.x - pose.x, goal.y - pose.y) < 0.4:
                continue
            if any(math.hypot(goal.x - p.x, goal.y - p.y) < 0.75 for p in self.explored_goals):
                continue
            if any(math.hypot(goal.x - p.x, goal.y - p.y) < 0.75 for p, _ in self._failed):
                continue
            goals.append(goal)
            sizes.append(len(rows))
            costs.append(float(distances[rows[index], cols[index]]))
        if not goals:
            return None
        if self.options.strategy == "frontier":
            return self._rank_frontiers(goals, sizes, pose, grid)[0]
        # Information per actual path metre; modest cluster bonus avoids distant detours.
        scores = [math.sqrt(size) / (cost + 0.5) for size, cost in zip(sizes, costs, strict=True)]
        return goals[int(np.argmax(scores))]

    def _record_arrival(self, goal: Vector3, before: OccupancyGrid) -> bool:
        self.mark_explored_goal(goal)
        current = self.latest_costmap
        if current is None or len(self.explored_goals) < self.options.min_goals:
            return False
        old = self._count_costmap_information(before)
        gain = 100 * (self._count_costmap_information(current) - old) / max(old, 1)
        self.no_gain_counter = self.no_gain_counter + 1 if gain < self.options.gain_percent else 0
        if self.no_gain_counter >= self.options.no_gain_attempts:
            self._status(
                "completed", f"Low map gain {gain:.2f}% for {self.no_gain_counter} arrivals"
            )
            return True
        return False

    def _navigate(self, goal: Vector3) -> bool:
        with self._guard:
            if self.stop_event.is_set():
                return False
            self._result = None
            self._wake.clear()
            self._awaiting_goal = True
            if not self._navigation.set_goal(PoseStamped(position=goal, frame_id="world")):
                self._awaiting_goal = False
                return False
        self._status(
            "moving",
            f"Target ({goal.x:.2f}, {goal.y:.2f}); check every {self.options.check_interval:g}s",
        )
        anchor = self.latest_odometry
        progress_time = time.monotonic()
        while not self.stop_event.is_set():
            self._wake.wait(self.options.check_interval)
            with self._guard:
                if self.stop_event.is_set():
                    break
                self._wake.clear()
                if self._result is not None:
                    self._awaiting_goal = False
                    if not self._result:
                        self._navigation.cancel_goal()
                    return self._result
            if not self._sensors_fresh():
                self._status("failed", "Odometry is stale or no map is available")
                self.stop_event.set()
                break
            pose = self.latest_odometry
            if (
                pose is not None
                and anchor is not None
                and math.hypot(
                    pose.position.x - anchor.position.x, pose.position.y - anchor.position.y
                )
                >= 0.15
            ):
                anchor, progress_time = pose, time.monotonic()
            if time.monotonic() - progress_time >= self.config.stall_timeout:
                self._status(
                    "retrying", f"No translation progress for {self.config.stall_timeout:g}s"
                )
                break
        with self._guard:
            self._awaiting_goal = False
            self._navigation.cancel_goal()
        return False

    def _run_exploration_loop(self) -> None:
        empty_attempts = 0
        try:
            while not self.stop_event.is_set():
                if not self._sensors_fresh():
                    self._status(
                        "failed", "A map and fresh odometry are required; restart exploration"
                    )
                    break
                grid, pose = self.latest_costmap, self.latest_odometry
                assert grid is not None and pose is not None
                self._status("selecting", "Finding reachable unvisited frontiers")
                goal = self.select_goal(pose.position, simple_inflate(grid, 0.25))
                if self.stop_event.is_set():
                    break
                if goal is None:
                    empty_attempts += 1
                    if empty_attempts >= 10:
                        self._status("completed", "No eligible reachable frontiers after 10 checks")
                        break
                    self._status("waiting", "No eligible frontier; waiting for map update")
                    self.stop_event.wait(self.options.check_interval)
                    continue
                empty_attempts = 0
                self._update_exploration_direction(pose.position, goal)
                if self._navigate(goal):
                    if self._record_arrival(goal, grid):
                        break
                elif not self.stop_event.is_set():
                    self._failed.append((goal, time.monotonic() + self.config.failure_cooldown))
                    self._status(
                        "retrying", "Navigation failed; target cooled down, selecting another"
                    )
        except Exception:
            logger.exception("Demo exploration failed")
            self._status("failed", "Unexpected exploration error; inspect backend log")
        finally:
            with self._guard:
                self.exploration_active = False
                self._awaiting_goal = False
                self.stop_event.set()
                self._navigation.cancel_goal()

    @rpc
    def explore(self) -> bool:
        with self._guard:
            if self.exploration_active or (
                self.exploration_thread is not None and self.exploration_thread.is_alive()
            ):
                return False
            self.reset_exploration_session()
            self._failed.clear()
            self.stop_event.clear()
            self.exploration_active = True
            self.exploration_thread = threading.Thread(target=self._exploration_loop, daemon=True)
            self.exploration_thread.start()
            return True

    def _exploration_loop(self) -> None:
        try:
            self._run_exploration_loop()
        finally:
            self.stop_tool("begin_demo_exploration")
            self.stop_tool("begin_exploration")

    @rpc
    def stop_exploration(self) -> bool:
        with self._guard:
            active = self.exploration_active
            self.exploration_active = False
            self.stop_event.set()
            self._wake.set()
            if active:
                self._awaiting_goal = False
                self._navigation.cancel_goal()
                self._status("stopped", "Stopped by user or movement safety signal")
            thread = self.exploration_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=DEFAULT_THREAD_JOIN_TIMEOUT)
            if thread.is_alive():
                logger.warning("Demo exploration worker is still finishing; new start blocked")
        return active

    @skill(uses=[CAP_MOVEMENT], lifecycle="background")
    def begin_demo_exploration(
        self,
        strategy: str = "frontier",
        min_goals: int = 10,
        gain_percent: float = 1.0,
        no_gain_attempts: int = 2,
        check_interval: float = 3.0,
    ) -> str:
        """Explore reachable building frontiers; use end_demo_exploration to stop.

        Args:
            strategy: frontier for original scoring; efficient for gain per reachable path metre.
            min_goals: Successful goals before checking low map gain; not a total goal limit.
            gain_percent: Minimum map information growth percent per successful trip.
            no_gain_attempts: Consecutive low-gain arrivals before ending.
            check_interval: Progress check seconds; failure feedback wakes immediately.
        """
        options = ExploreOptions.model_validate(
            {
                "strategy": strategy,
                "min_goals": min_goals,
                "gain_percent": gain_percent,
                "no_gain_attempts": no_gain_attempts,
                "check_interval": check_interval,
            }
        )
        with self._guard:
            if self.exploration_active or (
                self.exploration_thread is not None and self.exploration_thread.is_alive()
            ):
                return "Exploration is already active or stopping; stop it before restarting."
            self.options = options
            self.start_tool("begin_demo_exploration")
            self.explore()
        return f"Started demo exploration ({strategy}). Progress checks every {check_interval:g}s; explicit failures retry immediately."

    @skill
    def end_demo_exploration(self) -> str:
        """Stop demo exploration and cancel its active navigation goal."""
        self.stop_exploration()
        self.stop_tool("begin_demo_exploration")
        return "Demo exploration stopped."
