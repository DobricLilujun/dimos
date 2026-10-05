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
from typing import Any, Literal, Protocol

from dimos_lcm.std_msgs import Bool, String
import numpy as np
from pydantic import BaseModel, Field
from reactivex.disposable import Disposable
from scipy.ndimage import binary_dilation, label

from dimos.agents.annotation import skill
from dimos.agents.capabilities import CAP_MOVEMENT
from dimos.constants import DEFAULT_THREAD_JOIN_TIMEOUT
from dimos.core.core import rpc
from dimos.core.stream import In, Out
from dimos.mapping.occupancy.inflation import simple_inflate
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Twist import Twist
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.nav_msgs.OccupancyGrid import CostValues, OccupancyGrid
from dimos.msgs.sensor_msgs.PointCloud2 import PointCloud2
from dimos.navigation.experimental.frontier_exploration.wavefront_frontier_goal_selector import (
    WavefrontConfig,
    WavefrontFrontierExplorer,
)
from dimos.navigation.go2.replanning_a_star.spec import NavigationInterfaceSpec
from dimos.spec.utils import Spec
from dimos.utils.logging_config import setup_logger

logger = setup_logger()


class ExplorationMapSpec(Spec, Protocol):
    def navigation_ready(self) -> bool: ...


class ExploreOptions(BaseModel):
    strategy: Literal["frontier", "efficient"] = "frontier"
    min_goals: int = Field(default=10, ge=0, le=1000)
    gain_percent: float = Field(default=1.0, ge=0, le=100, allow_inf_nan=False)
    no_gain_attempts: int = Field(default=2, ge=1, le=100)
    check_interval: float = Field(default=3.0, ge=0.1, le=60, allow_inf_nan=False)


class DemoExplorerConfig(WavefrontConfig):
    frontier_inflation: float = Field(default=0.1, ge=0, le=1, allow_inf_nan=False)
    stall_timeout: float = Field(default=15.0, ge=3, le=300, allow_inf_nan=False)
    failure_cooldown: float = Field(default=60.0, ge=1, allow_inf_nan=False)
    sensor_timeout: float = Field(default=5.0, ge=1, allow_inf_nan=False)


def reachable_distances(grid: OccupancyGrid, pose: Vector3) -> np.ndarray[Any, Any]:
    """Dijkstra over free cells; diagonal steps cannot cut occupied corners."""
    free = grid.grid == CostValues.FREE
    distances = np.full(free.shape, np.inf)
    start = grid.world_to_grid(pose)
    x, y = math.floor(start.x), math.floor(start.y)
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
    _map_session: ExplorationMapSpec
    lidar: In[PointCloud2]
    nav_cmd_vel: Out[Twist]
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
        self._startup_origin: Vector3 | None = None
        self._startup_displacement = 0.0
        self._selection_reason = "No selection yet"
        self._lidar_received = 0.0
        self._costmap_received = 0.0
        self._bootstrapping = False

    @rpc
    def start(self) -> None:
        super().start()
        self.register_disposable(Disposable(self.lidar.subscribe(self._on_lidar)))

    def _on_lidar(self, cloud: PointCloud2) -> None:
        if len(cloud) and np.isfinite(cloud.points_f32()).all():
            self._lidar_received = time.monotonic()

    def _start_is_free(self) -> bool:
        grid, pose = self.latest_costmap, self.latest_odometry
        if grid is None or pose is None:
            return False
        inflated = simple_inflate(grid, self.config.frontier_inflation)
        cell = inflated.world_to_grid(pose.position)
        x, y = math.floor(cell.x), math.floor(cell.y)
        return (
            0 <= x < inflated.width
            and 0 <= y < inflated.height
            and inflated.grid[y, x] == CostValues.FREE
        )

    def _scan_blocker(self) -> str | None:
        if not self._map_session.navigation_ready():
            return "Map alignment or mapping safety gate is not ready"
        now = time.monotonic()
        if (
            not self._sensors_fresh()
            or now - self._odom_received > 1
            or now - self._lidar_received > 1
            or now - self._costmap_received > self.config.sensor_timeout
        ):
            return "Startup scan requires fresh lidar, odometry and costmap"
        grid, pose = self.latest_costmap, self.latest_odometry
        assert grid is not None and pose is not None
        cell = grid.world_to_grid(pose.position)
        radius = 0.55 / grid.resolution + 1 / math.sqrt(2)
        if not (radius <= cell.x < grid.width - radius and radius <= cell.y < grid.height - radius):
            return "Robot rotation clearance extends outside the map"
        yy, xx = np.indices(grid.grid.shape)
        distance = np.hypot(xx - cell.x, yy - cell.y) * grid.resolution
        disk = distance <= 0.55 + grid.resolution / math.sqrt(2)
        # Only tolerate the unobserved under-body area, never an occupied cell.
        ring = disk & (distance > 0.30 - grid.resolution / math.sqrt(2))
        if np.any(grid.grid[disk] > CostValues.FREE) or np.any(grid.grid[ring] != CostValues.FREE):
            return "Rotation clearance is occupied or unobserved"
        return None

    def _bootstrap_start(self) -> bool:
        if self._start_is_free():
            return True
        self._status("initializing", "Robot start cell invalid; collecting startup map")
        if self.stop_event.wait(1) or self.stop_event.is_set():
            return False
        if self._start_is_free():
            return True
        self._navigation.cancel_goal()
        deadline = time.monotonic() + 8
        with self._guard:
            if self.stop_event.is_set():
                return False
            self._bootstrapping = True
        try:
            self._status("initializing", "Slow in-place startup scan; no translation")
            while not self.stop_event.is_set():
                blocker = self._scan_blocker()
                if blocker:
                    self._status("blocked", blocker)
                    return False
                if self._start_is_free():
                    return True
                if time.monotonic() >= deadline:
                    self._status(
                        "blocked", "Robot start cell remains invalid after bounded startup scan"
                    )
                    return False
                with self._guard:
                    if self.stop_event.is_set():
                        return False
                    self.nav_cmd_vel.publish(Twist(angular=Vector3(0, 0, 0.15)))
                if self.stop_event.wait(0.1):
                    return False
        finally:
            with self._guard:
                self.nav_cmd_vel.publish(Twist())
                self._bootstrapping = False
        return False

    def _minimum_frontier_distance(self) -> float:
        if self._startup_displacement >= 2.0:
            return 0.4
        return 0.05 + 0.35 * self._startup_displacement / 2.0

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
                "minimum_frontier_distance_m": self._minimum_frontier_distance(),
                "startup_displacement_m": self._startup_displacement,
                **self.options.model_dump(),
            }

    def _on_costmap(self, msg: OccupancyGrid) -> None:
        self.latest_costmap = msg
        self._costmap_received = time.monotonic()

    def _on_odometry(self, msg: PoseStamped) -> None:
        self.latest_odometry = msg
        self._odom_received = time.monotonic()
        with self._guard:
            if self.exploration_active and self._startup_origin is not None:
                self._startup_displacement = max(
                    self._startup_displacement,
                    math.hypot(
                        msg.position.x - self._startup_origin.x,
                        msg.position.y - self._startup_origin.y,
                    ),
                )

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
        with self._guard:
            if self._startup_origin is None:
                self._startup_origin = Vector3(pose.x, pose.y, pose.z)
            minimum_distance = self._minimum_frontier_distance()
        startup = minimum_distance < 0.4
        reached_radius = 0.05 + 0.70 * min(self._startup_displacement / 2.0, 1.0)
        distances = reachable_distances(grid, pose)
        if not np.any(np.isfinite(distances)):
            self._selection_reason = (
                "Robot start cell is outside the map or not free after inflation"
            )
            return None
        frontier = np.isfinite(distances) & binary_dilation(
            grid.grid == CostValues.UNKNOWN, structure=np.ones((3, 3), dtype=bool)
        )
        raw_count = int(np.count_nonzero(frontier))
        self._failed = [(p, expiry) for p, expiry in self._failed if expiry > time.monotonic()]
        yy, xx = np.indices(frontier.shape)
        exclusions = [
            *((p, reached_radius) for p in self.explored_goals),
            *((p, 0.75) for p, _ in self._failed),
            (pose, minimum_distance),
        ]
        for previous, exclusion_radius in exclusions:
            cell = grid.world_to_grid(previous)
            radius = exclusion_radius / grid.resolution
            frontier &= (xx - cell.x) ** 2 + (yy - cell.y) ** 2 >= radius**2
        clusters, count = label(frontier)
        goals: list[Vector3] = []
        sizes: list[int] = []
        costs: list[float] = []
        small_clusters = 0
        short_clusters = 0
        for cluster in range(1, count + 1):
            rows, cols = np.where(clusters == cluster)
            if len(rows) * grid.resolution < self.config.min_frontier_perimeter:
                small_clusters += 1
                continue
            # Closest reachable representative, not a centroid in unknown space.
            cluster_distance = np.hypot(
                grid.origin.position.x + cols * grid.resolution - pose.x,
                grid.origin.position.y + rows * grid.resolution - pose.y,
            )
            navigable = np.flatnonzero(cluster_distance >= 0.25)
            index = (
                int(navigable[np.argmin(distances[rows[navigable], cols[navigable]])])
                if len(navigable)
                else int(np.argmin(distances[rows, cols]))
            )
            world = grid.grid_to_world((int(cols[index]), int(rows[index]), 0))
            goal = Vector3(world.x, world.y, 0)
            if math.hypot(goal.x - pose.x, goal.y - pose.y) < 0.25:
                extended = self._extend_short_frontier(pose, goal, grid, distances, reached_radius)
                if extended is None:
                    short_clusters += 1
                    continue
                goal = extended
            if math.hypot(goal.x - pose.x, goal.y - pose.y) < minimum_distance:
                continue
            if any(
                math.hypot(goal.x - p.x, goal.y - p.y) < reached_radius for p in self.explored_goals
            ):
                continue
            if any(math.hypot(goal.x - p.x, goal.y - p.y) < 0.75 for p, _ in self._failed):
                continue
            goals.append(goal)
            sizes.append(len(rows))
            cell = grid.world_to_grid(goal)
            costs.append(float(distances[int(cell.y), int(cell.x)]))
        if not goals:
            self._selection_reason = (
                f"No eligible frontier: reachable boundary cells={raw_count}, "
                f"after exclusions={int(np.count_nonzero(frontier))}, "
                f"small clusters={small_clusters}, short clusters without safe extension={short_clusters}, "
                f"cooling targets={len(self._failed)}, minimum distance={minimum_distance:.2f}m"
            )
            return None
        self._selection_reason = (
            f"Selected reachable goal; minimum distance={minimum_distance:.2f}m"
        )
        if startup:
            nearby = [index for index, cost in enumerate(costs) if cost <= 0.8]
            if nearby:
                return goals[min(nearby, key=lambda index: costs[index])]
        if self.options.strategy == "frontier":
            return self._rank_frontiers(goals, sizes, pose, grid)[0]
        # Information per actual path metre; modest cluster bonus avoids distant detours.
        scores = [math.sqrt(size) / (cost + 0.5) for size, cost in zip(sizes, costs, strict=True)]
        return goals[int(np.argmax(scores))]

    def _extend_short_frontier(
        self,
        pose: Vector3,
        frontier: Vector3,
        grid: OccupancyGrid,
        distances: np.ndarray[Any, Any],
        reached_radius: float,
    ) -> Vector3 | None:
        """Use nearby boundary direction, never a sub-tolerance or unknown navigation goal."""
        direction = np.array([frontier.x - pose.x, frontier.y - pose.y])
        length = float(np.linalg.norm(direction))
        if length < 1e-6:
            return None
        direction /= length
        rows, cols = np.where(np.isfinite(distances) & (distances <= 0.8))
        dx = grid.origin.position.x + cols * grid.resolution - pose.x
        dy = grid.origin.position.y + rows * grid.resolution - pose.y
        separation = np.hypot(dx, dy)
        forward = dx * direction[0] + dy * direction[1]
        eligible = (separation >= 0.25) & (forward >= separation * 0.85)
        for previous, radius in [
            *((p, reached_radius) for p in self.explored_goals),
            *((p, 0.75) for p, _ in self._failed),
        ]:
            eligible &= np.hypot(dx + pose.x - previous.x, dy + pose.y - previous.y) >= radius
        indices = np.flatnonzero(eligible)
        if len(indices) == 0:
            return None
        index = int(indices[np.argmin(distances[rows[indices], cols[indices]])])
        return grid.grid_to_world((int(cols[index]), int(rows[index]), 0))

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
            f"Target ({goal.x:.2f}, {goal.y:.2f}); check every {self.options.check_interval:g}s; "
            f"frontier threshold={self._minimum_frontier_distance():.2f}m",
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
            if not self._bootstrap_start():
                return
            while not self.stop_event.is_set():
                if not self._sensors_fresh():
                    self._status(
                        "failed", "A map and fresh odometry are required; restart exploration"
                    )
                    break
                grid, pose = self.latest_costmap, self.latest_odometry
                assert grid is not None and pose is not None
                self._status("selecting", "Finding reachable unvisited frontiers")
                goal = self.select_goal(
                    pose.position, simple_inflate(grid, self.config.frontier_inflation)
                )
                if self.stop_event.is_set():
                    break
                if goal is None:
                    if not self._start_is_free():
                        self._status("blocked", self._selection_reason)
                        break
                    empty_attempts += 1
                    if empty_attempts >= 10:
                        self._status(
                            "completed",
                            f"No eligible reachable frontiers after 10 checks. {self._selection_reason}",
                        )
                        break
                    self._status("waiting", self._selection_reason + "; waiting for map update")
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
            if self._bootstrapping:
                self.nav_cmd_vel.publish(Twist())
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
        """Explore reachable frontiers, with one bounded startup scan if needed.

        May turn slowly in place when the start cell is invalid and clearance is known.
        Use end_demo_exploration to stop both startup scanning and navigation.

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
            self._startup_origin = (
                Vector3(
                    self.latest_odometry.position.x,
                    self.latest_odometry.position.y,
                    self.latest_odometry.position.z,
                )
                if self.latest_odometry is not None
                else None
            )
            self._startup_displacement = 0.0
            self.start_tool("begin_demo_exploration")
            self.explore()
        return f"Started demo exploration ({strategy}). Progress checks every {check_interval:g}s; explicit failures retry immediately."

    @skill
    def end_demo_exploration(self) -> str:
        """Stop demo exploration and cancel its active navigation goal."""
        self.stop_exploration()
        self.stop_tool("begin_demo_exploration")
        return "Demo exploration stopped."
