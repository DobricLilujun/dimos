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
    # Greedy: always take the closest reachable frontier instead of a distant
    # detour, so the robot never circles back over covered ground.
    greedy: bool = True
    max_leg_distance_m: float = Field(default=1.5, ge=0.3, le=10, allow_inf_nan=False)
    # Coverage completion: only end when this fraction of reachable free cells
    # has been visited, so the run never stops early on a low-gain plateau.
    coverage_threshold: float = Field(default=0.9, ge=0, le=1, allow_inf_nan=False)
    # Keep moving: recover a stall (turn / back up) this many times before the
    # target is cooled and a new frontier is chosen instead of quitting.
    stall_recovery_attempts: int = Field(default=3, ge=1, le=10, allow_inf_nan=False)


class DemoExplorerConfig(WavefrontConfig):
    frontier_inflation: float = Field(default=0.1, ge=0, le=1, allow_inf_nan=False)
    stall_timeout: float = Field(default=15.0, ge=3, le=300, allow_inf_nan=False)
    failure_cooldown: float = Field(default=60.0, ge=1, allow_inf_nan=False)
    sensor_timeout: float = Field(default=5.0, ge=1, allow_inf_nan=False)
    # Static/moving detection: recovery actions for a stuck robot.
    stall_recovery: bool = True
    recovery_turn_speed: float = Field(default=0.5, ge=0.05, le=2.0, allow_inf_nan=False)
    recovery_turn_duration: float = Field(default=1.0, ge=0.1, le=10.0, allow_inf_nan=False)
    recovery_back_speed: float = Field(default=0.2, ge=0.05, le=1.0, allow_inf_nan=False)
    recovery_back_distance: float = Field(default=0.4, ge=0.05, le=3.0, allow_inf_nan=False)


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
        self._costmap_revision = 0
        self._lidar_revision = 0
        self._lidar_map_consistent = False
        self._lidar_timestamp = float("-inf")
        self._closure_revision: tuple[int, int] | None = None
        self._closure_updates = 0
        self._closure_since: float | None = None

    @rpc
    def start(self) -> None:
        super().start()
        self.register_disposable(Disposable(self.lidar.subscribe(self._on_lidar)))

    def _on_lidar(self, cloud: PointCloud2) -> None:
        points = cloud.points_f32()
        if len(cloud) and np.isfinite(points).all():
            if cloud.ts is None:
                self._lidar_received = time.monotonic()
                return
            if not math.isfinite(cloud.ts) or cloud.ts <= self._lidar_timestamp:
                return
            self._lidar_received = time.monotonic()
            self._lidar_timestamp = cloud.ts
            self._lidar_revision += 1
            self._lidar_map_consistent = False
            grid = self.latest_costmap
            if grid is not None and cloud.frame_id == grid.frame_id == "world":
                cols = np.floor((points[:, 0] - grid.origin.position.x) / grid.resolution).astype(
                    int
                )
                rows = np.floor((points[:, 1] - grid.origin.position.y) / grid.resolution).astype(
                    int
                )
                inside = (cols >= 0) & (cols < grid.width) & (rows >= 0) & (rows < grid.height)
                # Static fusion can pause while fresh lidar still proves that the
                # saved enclosure has not exposed unknown or out-of-map space.
                self._lidar_map_consistent = bool(
                    len(points) >= 32
                    and np.all(inside)
                    and np.all(grid.grid[rows, cols] != CostValues.UNKNOWN)
                )

    def _log_invalid_start(
        self,
        pose: Vector3,
        raw: OccupancyGrid,
        inflated: OccupancyGrid,
        routing: OccupancyGrid,
        *,
        stage: str,
    ) -> None:
        def sample(grid: OccupancyGrid) -> dict[str, Any]:
            cell = grid.world_to_grid(pose)
            x, y = math.floor(cell.x), math.floor(cell.y)
            inside = 0 <= x < grid.width and 0 <= y < grid.height
            value = int(grid.grid[y, x]) if inside else None
            state = (
                "outside_map"
                if value is None
                else "unknown"
                if value == CostValues.UNKNOWN
                else "free"
                if value == CostValues.FREE
                else "occupied"
                if value >= CostValues.OCCUPIED
                else "non_free_cost"
            )
            return {"cell": [x, y], "value": value, "state": state}

        now = time.monotonic()
        raw_start, inflated_start, routing_start = sample(raw), sample(inflated), sample(routing)
        causes = []
        if raw_start["state"] != "free":
            causes.append(f"raw_start_{raw_start['state']}")
        if inflated_start["state"] != "free" and raw_start["state"] == "free":
            causes.append("obstacle_inflation")
        if routing_start["state"] != "free" and inflated_start["state"] == "free":
            causes.append("failed_goal_exclusion")
        odom = self.latest_odometry
        if odom is not None and odom.frame_id != raw.frame_id:
            causes.append("frame_mismatch")
        failed_zones = []
        for previous, expiry in self._failed:
            cell = routing.world_to_grid(previous)
            x, y = routing_start["cell"]
            covers_start = (x - cell.x) ** 2 + (y - cell.y) ** 2 < (0.75 / routing.resolution) ** 2
            if expiry > now and covers_start:
                failed_zones.append(
                    {
                        "goal_xyz": [previous.x, previous.y, previous.z],
                        "distance_m": math.hypot(previous.x - pose.x, previous.y - pose.y),
                        "remaining_s": round(expiry - now, 3),
                    }
                )
        x, y = raw_start["cell"]
        patch = (
            raw.grid[
                max(0, y - 2) : min(raw.height, y + 3), max(0, x - 2) : min(raw.width, x + 3)
            ].tolist()
            if raw_start["state"] != "outside_map"
            else []
        )
        logger.warning(
            "Exploration start cell invalid",
            stage=stage,
            causes=causes,
            robot_xyz=[pose.x, pose.y, pose.z],
            odom_frame=odom.frame_id if odom is not None else None,
            map_frame=raw.frame_id,
            map_origin_xyz=[
                raw.origin.position.x,
                raw.origin.position.y,
                raw.origin.position.z,
            ],
            map_origin_yaw=float(raw.origin.orientation.euler[2]),
            map_size_cells=[raw.width, raw.height],
            resolution_m=raw.resolution,
            inflation_radius_m=self.config.frontier_inflation,
            raw_start=raw_start,
            inflated_start=inflated_start,
            routing_start=routing_start,
            raw_neighborhood=patch,
            failed_exclusion_radius_m=0.75,
            covering_failed_zones=failed_zones,
            map_revision=self._costmap_revision,
            costmap_age_s=round(now - self._costmap_received, 3)
            if self._costmap_received
            else None,
            odom_age_s=round(now - self._odom_received, 3) if self._odom_received else None,
            lidar_age_s=round(now - self._lidar_received, 3) if self._lidar_received else None,
        )

    def _start_is_free(self, *, log_failure: bool = True) -> bool:
        grid, pose = self.latest_costmap, self.latest_odometry
        if grid is None or pose is None:
            return False
        inflated = simple_inflate(grid, self.config.frontier_inflation)
        cell = inflated.world_to_grid(pose.position)
        x, y = math.floor(cell.x), math.floor(cell.y)
        valid = (
            0 <= x < inflated.width
            and 0 <= y < inflated.height
            and inflated.grid[y, x] == CostValues.FREE
        )
        if not valid and log_failure:
            self._log_invalid_start(pose.position, grid, inflated, inflated, stage="start_check")
        return valid

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
        if self._start_is_free(log_failure=False):
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
                if self._start_is_free(log_failure=False):
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
        self._costmap_revision += 1

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
            and time.monotonic() - self._lidar_received <= self.config.sensor_timeout
        )

    def select_goal(
        self, pose: Vector3, grid: OccupancyGrid, *, raw_grid: OccupancyGrid | None = None
    ) -> Vector3 | None:
        inflated = grid
        with self._guard:
            if self._startup_origin is None:
                self._startup_origin = Vector3(pose.x, pose.y, pose.z)
            minimum_distance = self._minimum_frontier_distance()
        startup = minimum_distance < 0.4
        reached_radius = 0.05 + 0.70 * min(self._startup_displacement / 2.0, 1.0)
        self._failed = [(p, expiry) for p, expiry in self._failed if expiry > time.monotonic()]
        if self._failed:
            grid = grid.copy()
            yy, xx = np.indices(grid.grid.shape)
            for previous, _ in self._failed:
                cell = grid.world_to_grid(previous)
                blocked = (xx - cell.x) ** 2 + (yy - cell.y) ** 2 < (0.75 / grid.resolution) ** 2
                grid.grid[blocked] = CostValues.OCCUPIED
        distances = reachable_distances(grid, pose)
        if not np.any(np.isfinite(distances)):
            self._log_invalid_start(
                pose,
                raw_grid if raw_grid is not None else inflated,
                inflated,
                grid,
                stage="goal_selection",
            )
            self._selection_reason = (
                "Robot start cell is outside the map or not free after inflation"
            )
            return None
        frontier = np.isfinite(distances) & binary_dilation(
            grid.grid == CostValues.UNKNOWN, structure=np.ones((3, 3), dtype=bool)
        )
        raw_count = int(np.count_nonzero(frontier))
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
        selected: Vector3 | None = None
        if startup:
            nearby = [index for index, cost in enumerate(costs) if cost <= 0.8]
            if nearby:
                selected = goals[min(nearby, key=lambda index: costs[index])]
        # Greedy: take the closest reachable frontier (minimum path cost) so the
        # robot never takes a long detour or circles back over covered ground.
        if selected is None:
            if self.options.greedy:
                selected = goals[min(range(len(goals)), key=lambda index: costs[index])]
            elif self.options.strategy == "frontier":
                selected = self._rank_frontiers(goals, sizes, pose, grid)[0]
            else:
                scores = [
                    math.sqrt(size) / (cost + 0.5) for size, cost in zip(sizes, costs, strict=True)
                ]
                selected = goals[int(np.argmax(scores))]
        return self._short_leg(pose, selected, grid, distances)

    def _short_leg(
        self, pose: Vector3, goal: Vector3, grid: OccupancyGrid, distances: np.ndarray[Any, Any]
    ) -> Vector3 | None:
        """Walk the shortest-path distance field back to a bounded local waypoint."""
        cell = grid.world_to_grid(goal)
        row, col = math.floor(cell.y), math.floor(cell.x)
        limit = self.options.max_leg_distance_m
        while distances[row, col] > limit:
            neighbours = []
            for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
                ny, nx = row + dy, col + dx
                if not (0 <= ny < grid.height and 0 <= nx < grid.width):
                    continue
                if (
                    dx
                    and dy
                    and (
                        grid.grid[row, nx] != CostValues.FREE
                        or grid.grid[ny, col] != CostValues.FREE
                    )
                ):
                    continue
                step = grid.resolution * (math.sqrt(2) if dx and dy else 1)
                if math.isclose(distances[ny, nx] + step, distances[row, col]):
                    neighbours.append((float(distances[ny, nx]), ny, nx))
            if not neighbours:
                self._selection_reason = "No safe connector to local frontier waypoint"
                return None
            _, row, col = min(neighbours)
        waypoint = grid.grid_to_world((col, row, 0))
        if math.hypot(waypoint.x - pose.x, waypoint.y - pose.y) < 0.25:
            self._selection_reason = "Local waypoint is below navigation arrival tolerance"
            return None
        self._selection_reason += f"; leg path={distances[row, col]:.2f}m (limit {limit:g}m)"
        return Vector3(waypoint.x, waypoint.y, 0)

    def _coverage_fraction(self, grid: OccupancyGrid, pose: Vector3) -> float:
        """Visited-cell diagnostic; coverage alone is not proof of enclosure."""
        distances = reachable_distances(grid, pose)
        reachable = np.isfinite(distances)
        free = reachable & (grid.grid == CostValues.FREE)
        total = int(np.count_nonzero(free))
        if total == 0:
            return 0.0
        yy, xx = np.indices(free.shape)
        visited = np.zeros_like(free, dtype=bool)
        for goal in self.explored_goals:
            cell = grid.world_to_grid(goal)
            radius = 0.5 / grid.resolution
            visited |= (xx - cell.x) ** 2 + (yy - cell.y) ** 2 <= radius**2
        return float(np.count_nonzero(free & visited)) / total

    def _has_reachable_frontier(self, grid: OccupancyGrid, pose: Vector3) -> bool:
        distances = reachable_distances(grid, pose)
        frontier = np.isfinite(distances) & binary_dilation(
            grid.grid == CostValues.UNKNOWN, structure=np.ones((3, 3), dtype=bool)
        )
        return bool(np.any(frontier))

    def _is_exploration_complete(self, grid: OccupancyGrid, pose: Vector3) -> bool:
        """Require a stable observed enclosure, not exhaustion of filtered goals."""
        reachable = np.isfinite(reachable_distances(grid, pose.position))
        open_edge = (
            np.any(reachable[0])
            or np.any(reachable[-1])
            or np.any(reachable[:, 0])
            or np.any(reachable[:, -1])
        )
        if (
            not self._sensors_fresh()
            or (self._lidar_revision > 0 and not self._lidar_map_consistent)
            or (
                time.monotonic() - self._costmap_received > self.config.sensor_timeout
                and not self._lidar_map_consistent
            )
            or not self._map_session.navigation_ready()
            or not np.any(reachable)
            or open_edge
            or self._has_reachable_frontier(grid, pose.position)
        ):
            self._closure_since = None
            self._closure_updates = 0
            self._closure_revision = None
            return False
        now = time.monotonic()
        if self._closure_since is None:
            self._closure_since = now
        revision = (
            self._costmap_revision,
            self._lidar_revision if self._lidar_map_consistent else 0,
        )
        if self._closure_revision != revision:
            self._closure_updates += 1
            self._closure_revision = revision
        return self._closure_updates >= 3 and now - self._closure_since >= 5

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
                "selecting", f"Low map gain {gain:.2f}%; checking remaining reachable frontiers"
            )
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
        recovery_attempts = 0
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
                # Static/moving recovery: the robot is stuck, so turn or back up
                # to free it before giving up on this target. Never just stop.
                if (
                    self.config.stall_recovery
                    and recovery_attempts < self.options.stall_recovery_attempts
                ):
                    recovery_attempts += 1
                    recovered = self._recover_stuck()
                else:
                    recovered = False
                if recovered:
                    with self._guard:
                        if self.stop_event.is_set():
                            break
                        self._result = None
                        self._awaiting_goal = True
                        if not self._navigation.set_goal(
                            PoseStamped(position=goal, frame_id="world")
                        ):
                            self._awaiting_goal = False
                            break
                    anchor, progress_time = pose, time.monotonic()
                    continue
                break
        with self._guard:
            self._awaiting_goal = False
            self._navigation.cancel_goal()
        return False

    def _recover_stuck(self) -> bool:
        """Free a stuck robot by turning, then backing up.

        Returns True when a recovery made the robot move (so the caller keeps
        the current target); False when nothing moved, so the target is cooled
        and a new frontier is chosen instead. This is the static/moving
        detection: a prolonged no-motion episode triggers a turn / back-up
        rather than quitting, so exploration keeps moving.
        """
        if not self._map_session.navigation_ready():
            return False
        if self._scan_blocker() is not None:
            self._status(
                "blocked", "Recovery requires fresh sensors and observed rotation clearance"
            )
            return False
        with self._guard:
            if self.stop_event.is_set():
                return False
            start = self.latest_odometry
        if start is None:
            return False
        with self._guard:
            self._awaiting_goal = False
        self._navigation.cancel_goal()
        # Step 1: a slow in-place rotation sweep (turn to a new view).
        self._status("recovering", "Stuck; turning in place to escape")
        deadline = time.monotonic() + self.config.recovery_turn_duration
        try:
            while not self.stop_event.is_set() and time.monotonic() < deadline:
                if self._scan_blocker() is not None:
                    return False
                with self._guard:
                    self.nav_cmd_vel.publish(
                        Twist(angular=Vector3(0, 0, self.config.recovery_turn_speed))
                    )
                if self.stop_event.wait(0.1):
                    return False
        finally:
            with self._guard:
                self.nav_cmd_vel.publish(Twist())
        moved = self._moved_since(start)
        if moved:
            self._status("moving", "Recovery turn freed the robot")
            return True
        # Step 2: still stuck, back up a short distance.
        self._status("recovering", "Still stuck; backing up")
        pose = self.latest_odometry
        costmap = self.latest_costmap
        if pose is None or costmap is None:
            return False
        yaw = pose.orientation.to_euler().z
        inflated = simple_inflate(costmap, 0.55)
        distances = reachable_distances(inflated, pose.position)
        for distance in np.linspace(0, self.config.recovery_back_distance, 10):
            point = Vector3(
                pose.position.x - math.cos(yaw) * distance,
                pose.position.y - math.sin(yaw) * distance,
                0,
            )
            cell = inflated.world_to_grid(point)
            x, y = math.floor(cell.x), math.floor(cell.y)
            if not (
                0 <= x < inflated.width
                and 0 <= y < inflated.height
                and np.isfinite(distances[y, x])
            ):
                self._status("blocked", "Recovery back-up leaves observed free clearance")
                return False
        deadline = time.monotonic() + self.config.recovery_back_distance / max(
            self.config.recovery_back_speed, 1e-3
        )
        try:
            while not self.stop_event.is_set() and time.monotonic() < deadline:
                if self._scan_blocker() is not None:
                    return False
                with self._guard:
                    self.nav_cmd_vel.publish(
                        Twist(linear=Vector3(-self.config.recovery_back_speed, 0, 0))
                    )
                if self.stop_event.wait(0.1):
                    return False
        finally:
            with self._guard:
                self.nav_cmd_vel.publish(Twist())
        moved = self._moved_since(start)
        if moved:
            self._status("moving", "Recovery back-up freed the robot")
            return True
        self._status("retrying", "Recovery did not free the robot; choosing another target")
        return False

    def _moved_since(self, start: PoseStamped) -> bool:
        now = self.latest_odometry
        if now is None:
            return False
        return (
            math.hypot(now.position.x - start.position.x, now.position.y - start.position.y) >= 0.1
        )

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
                    pose.position,
                    simple_inflate(grid, self.config.frontier_inflation),
                    raw_grid=grid,
                )
                if self.stop_event.is_set():
                    break
                if goal is None:
                    if not self._start_is_free():
                        self._status("blocked", self._selection_reason)
                        break
                    # Never stop early: only end when the reachable free space
                    # has been swept (coverage), or a coverage-based completion
                    # passes. Otherwise keep waiting for a map update and try
                    # again, so the robot keeps moving until it is truly done.
                    if self._is_exploration_complete(grid, pose):
                        self._status(
                            "completed",
                            "Reachable free space swept; exploration complete",
                        )
                        break
                    empty_attempts += 1
                    self._status(
                        "waiting",
                        f"No new frontier yet (check {empty_attempts}); "
                        + self._selection_reason
                        + "; staying in the loop, waiting for a map update",
                    )
                    self.stop_event.wait(self.options.check_interval)
                    continue
                empty_attempts = 0
                self._update_exploration_direction(pose.position, goal)
                if self._navigate(goal):
                    # Arrival updates the map-gain counter; completion is now
                    # coverage-based, so the robot keeps moving until the
                    # reachable free space has actually been swept.
                    self._record_arrival(goal, grid)
                    if self._is_exploration_complete(grid, pose):
                        self._status("completed", "Reachable free space swept; complete")
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
            self._closure_since = None
            self._closure_updates = 0
            self._closure_revision = None
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
        greedy: bool = True,
        coverage_threshold: float = 0.9,
        stall_recovery_attempts: int = 3,
        max_leg_distance_m: float = 1.5,
    ) -> str:
        """Explore in short known-free legs until fresh maps confirm no reachable frontiers.

        Greedy mode takes the closest reachable frontier so the robot never
        takes a long detour or circles over covered ground; when it is stuck it
        attempts clearance-checked recovery. Completion needs three distinct
        map/lidar observations spanning five seconds with no reachable unknown boundary;
        low gain and filtered/cooling goals alone are not completion.
        May turn slowly in place when the start cell is invalid.
        Use end_demo_exploration to stop.

        Args:
            strategy: frontier for original scoring; efficient for gain per reachable path metre.
            min_goals: Successful goals before low-gain checks; not a total goal limit.
            gain_percent: Minimum map information growth percent per successful trip.
            no_gain_attempts: Consecutive low-gain arrivals before the gain counter resets.
            check_interval: Progress check seconds; failure feedback wakes immediately.
            greedy: Take the closest reachable frontier instead of a distant detour.
            max_leg_distance_m: Maximum known-free path length per navigation leg.
            coverage_threshold: Legacy coverage diagnostic; cannot override reachable frontiers.
            stall_recovery_attempts: Turn/back-up attempts before a stuck target is cooled.
        """
        options = ExploreOptions.model_validate(
            {
                "strategy": strategy,
                "min_goals": min_goals,
                "gain_percent": gain_percent,
                "no_gain_attempts": no_gain_attempts,
                "check_interval": check_interval,
                "greedy": greedy,
                "max_leg_distance_m": max_leg_distance_m,
                "coverage_threshold": coverage_threshold,
                "stall_recovery_attempts": stall_recovery_attempts,
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
