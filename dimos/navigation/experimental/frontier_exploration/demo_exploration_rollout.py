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

"""How much ground an exploration policy re-walks, measured offline.

`demo_` prefixed on purpose: this is an experiment runner, not a test suite. It drives
the real selector in :mod:`wavefront_frontier_goal_selector` over the real procedural
scene geometry (``dimos/simulation/scenes``) with a height-blind 2D lidar, a believed
map that starts fully unknown, and an A* walker. Physics is left out on purpose: the
legged sim's robot model lives in LFS, and control-level slips do not decide whether a
policy keeps crossing ground it already covered.

    uv run python -m dimos.navigation.experimental.frontier_exploration.demo_exploration_rollout

Read the table as: how long to map the building, how far the robot walked to do it, and
what share of that walking was over ground it had already walked.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
import heapq
import math
from typing import Any

import numpy as np

from dimos.mapping.occupancy.inflation import simple_inflate
from dimos.msgs.geometry_msgs.Pose import Pose
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.nav_msgs.OccupancyGrid import CostValues, OccupancyGrid
from dimos.navigation.experimental.frontier_exploration.wavefront_frontier_goal_selector import (
    WavefrontConfig,
    WavefrontFrontierExplorer,
    _nearest_free_cell,
)
from dimos.simulation.scenes.procedural import Scene, corridor_turn, office, stairs

RES = 0.1  # map resolution in metres
INFLATION = 0.25  # the radius the exploration loop inflates its costmap by
SENSOR = (0.15, 0.6)  # the band a 2D lidar sees: it misses ceilings and reads step faces
BODY_RADIUS = 0.18  # the Go2's half width, used to inflate obstacles
BLOCK = round(0.5 / RES)  # the block size the re-walk metric is measured on
SPEED = 0.4  # m/s, the exploration speed the stacks use
SCAN_EVERY = 0.4  # metres of travel between lidar scans
RAYS = 90  # a spinning lidar at about 4 degrees
RANGE = 8.0  # lidar range in metres
HORIZON = 30 * SPEED  # metres per goal attempt, the local planner's patience

FREE = int(CostValues.FREE)
UNKNOWN = int(CostValues.UNKNOWN)
WALL = int(CostValues.OCCUPIED)


@dataclass
class World:
    """Ground truth as a height-blind 2D lidar would report it."""

    shape: tuple[int, int]
    solid: np.ndarray  # obstacles a lidar at SENSOR height can see
    free: np.ndarray  # the cells worth mapping
    start: tuple[float, float]


def raster(scene: Scene, *, stairs_free: bool = False) -> World:
    """Rasterize a scene, keeping only geometry the lidar band can see.

    ``stairs_free`` stands in for the 2.5D traversability layer that does not exist
    yet: with it, a flight of steps is drivable instead of a wall.
    """

    lo, hi = scene.bounds()
    origin_x, origin_y = float(lo[0]), float(lo[1])
    cols = math.ceil((hi[0] - origin_x) / RES) + 1
    rows = math.ceil((hi[1] - origin_y) / RES) + 1
    solid = np.zeros((rows, cols), dtype=bool)
    for box in scene.boxes:
        if box.half[2] <= 0:
            continue
        low = (box.center[0] - box.half[0], box.center[1] - box.half[1])
        high = (box.center[0] + box.half[0], box.center[1] + box.half[1])
        c0 = max(0, int((low[0] - origin_x) / RES))
        c1 = min(cols, math.ceil((high[0] - origin_x) / RES))
        r0 = max(0, int((low[1] - origin_y) / RES))
        r1 = min(rows, math.ceil((high[1] - origin_y) / RES))
        if c1 <= c0 or r1 <= r0:
            continue
        visible = (
            box.center[2] - box.half[2] <= SENSOR[1] and box.center[2] + box.half[2] >= SENSOR[0]
        )
        if not visible:
            continue
        is_step = box.kind == "stair"
        if is_step and stairs_free:
            continue
        solid[r0:r1, c0:c1] = True

    # Ground truth is not inflated: the map the policy plans over is inflated once,
    # in Agent.costmap, the way the stack inflates its costmap.
    free = ~solid
    # A body at the start must have somewhere to stand, or the scene is not runnable.
    sx, sy = scene.start[0], scene.start[1]
    start = (sx, sy)
    if not free[int((sy - origin_y) / RES), int((sx - origin_x) / RES)]:
        raise ValueError(f"start of {scene.name} lands inside an obstacle")
    return World(shape=(rows, cols), solid=solid, free=free, start=start)


def inflate(mask: np.ndarray, cells: int) -> np.ndarray:
    if cells <= 0:
        return mask
    padded = np.pad(mask, cells)
    out = np.zeros_like(mask, dtype=bool)
    for dy in range(2 * cells + 1):
        for dx in range(2 * cells + 1):
            out |= padded[dy : dy + mask.shape[0], dx : dx + mask.shape[1]]
    return out


def grid_of(world: World, values: np.ndarray, *, inflated: bool) -> OccupancyGrid:
    return OccupancyGrid(
        grid=values.astype(np.int8),
        resolution=RES,
        origin=Pose(),
        frame_id="world",
    )


class Agent:
    """Belief, sensor, and a walker. Everything the policy is not."""

    def __init__(self, world: World) -> None:
        self.world = world
        rows, cols = world.shape
        self.belief = np.full((rows, cols), UNKNOWN, dtype=np.int8)
        self.pose = Vector3(world.start[0], world.start[1], 0.0)
        self.yaw = 0.0
        self.visited = np.zeros((rows, cols), dtype=bool)
        # Re-walked ground is judged on half-metre blocks: crossing the same corridor
        # twice is re-walking, even if the exact 10 cm cells differ.
        self.blocks = np.zeros((math.ceil(rows / BLOCK), math.ceil(cols / BLOCK)), dtype=bool)
        self.distance = 0.0
        self.rewalked = 0.0
        self.time = 0.0
        self.attempts = 0
        self.failures = 0
        self.scan()

    def scan(self) -> None:
        """A 360 degree scan, stopping at the first solid cell along each ray.

        Vectorized per ray: the rollout does thousands of scans, and a per-sample
        Python loop makes a single episode take minutes instead of seconds.
        """
        rows, cols = self.world.shape
        steps = math.ceil(RANGE / (RES / 2))
        offsets = np.arange(1, steps + 1) * (RES / 2)
        for step in range(RAYS):
            heading = 2 * math.pi * step / RAYS
            xs = self.pose.x + math.cos(heading) * offsets
            ys = self.pose.y + math.sin(heading) * offsets
            cs = (xs / RES).astype(np.int32)
            rs = (ys / RES).astype(np.int32)
            inside = (rs >= 0) & (rs < rows) & (cs >= 0) & (cs < cols)
            if not inside.any():
                continue
            cut = int(np.argmax(~inside)) if not inside.all() else len(inside)
            rs, cs = rs[:cut], cs[:cut]
            hit = self.world.solid[rs, cs]
            stop = int(np.argmax(hit)) if hit.any() else len(rs)
            self.belief[rs[: stop + 1], cs[: stop + 1]] = FREE
            if hit.any():
                self.belief[rs[stop], cs[stop]] = WALL

    def costmap(self) -> OccupancyGrid:
        """The inflated map the explorer plans over, unknown kept as unknown.

        The selector finds frontiers by looking for free cells beside unknown ones, so
        flattening unknown into wall here would leave it with nothing to explore.
        """
        # The same inflation the exploration loop applies to its costmap.
        return simple_inflate(grid_of(self.world, self.belief.copy(), inflated=True), 0.25)

    def walk(self, goal: Vector3) -> bool:
        """Follow A* over the believed map. False when the goal cannot be reached.

        Unknown is not drivable, which is what makes a frontier behind a wall a
        failed attempt rather than a route. A failed attempt still costs the
        seconds the planner spends looking for one.
        """
        # Unknown is drivable here, because that is what a flat 2D costmap says: the
        # navigator will happily head into space it has never seen.
        path = astar(self.costmap().grid, (self.pose.y, self.pose.x), (goal.y, goal.x))
        if path is None:
            self.attempts += 1
            self.failures += 1
            return False
        self.attempts += 1
        for row, col in path[1:]:
            x, y = (col + 0.5) * RES, (row + 0.5) * RES
            step = math.hypot(x - self.pose.x, y - self.pose.y)
            if step == 0.0:
                continue
            if math.hypot(self.pose.x - goal.x, self.pose.y - goal.y) < 0.2:
                return True
            self.distance += step
            self.time += step / SPEED
            self.yaw = math.atan2(y - self.pose.y, x - self.pose.x)
            self.pose = Vector3(x, y, 0.0)
            block = (row // BLOCK, col // BLOCK)
            if self.blocks[block]:
                self.rewalked += step
            self.blocks[block] = True
            self.visited[row, col] = True
            if self.world.solid[row, col]:  # a real collision, not a map artifact
                self.failures += 1
                return False
            if step and math.floor(self.distance / SCAN_EVERY) > math.floor(
                (self.distance - step) / SCAN_EVERY
            ):
                self.scan()
        self.scan()
        return math.hypot(self.pose.x - goal.x, self.pose.y - goal.y) < 0.5

    def covered(self) -> float:
        """Fraction of the ground-truth free area the belief has resolved."""
        known = self.belief != UNKNOWN
        return float(np.count_nonzero(self.world.free & known) / np.count_nonzero(self.world.free))


def astar(
    belief: np.ndarray, start: tuple[float, float], goal: tuple[float, float]
) -> list[tuple[int, int]] | None:
    """8-neighbour A* over cells the robot currently believes are free."""

    rows, cols = belief.shape
    s = (int(start[0] / RES), int(start[1] / RES))
    g = (int(goal[0] / RES), int(goal[1] / RES))
    if not (0 <= s[0] < rows and 0 <= s[1] < cols):
        return None
    if belief[s] == WALL:
        # Standing in the inflation buffer of an obstacle is normal while passing it.
        nearby = _nearest_free_cell(belief != WALL, s[0], s[1], RES)
        if nearby is None:
            return None
        s = nearby
    if not (0 <= g[0] < rows and 0 <= g[1] < cols):
        return None
    open_set: list[tuple[float, float, tuple[int, int]]] = [(0.0, 0.0, s)]
    came_from: dict[tuple[int, int], tuple[int, int]] = {}
    cost = {s: 0.0}
    while open_set:
        _, so_far, cell = heapq.heappop(open_set)
        if cell == g:
            route = [cell]
            while cell in came_from:
                cell = came_from[cell]
                route.append(cell)
            return route[::-1]
        if so_far > cost.get(cell, math.inf):
            continue
        row, col = cell
        for dy, dx in (
            (1, 0),
            (-1, 0),
            (0, 1),
            (0, -1),
            (1, 1),
            (1, -1),
            (-1, 1),
            (-1, -1),
        ):
            step_cost = math.hypot(dy, dx) * RES
            for ny, nx in free_neighbours(belief, row, col, dy, dx):
                key = (ny, nx)
                through = cost[(row, col)] + step_cost
                if through < cost.get(key, math.inf):
                    came_from[key] = (row, col)
                    cost[key] = through
                    heapq.heappush(
                        open_set, (through + math.hypot(ny - g[0], nx - g[1]), through, key)
                    )
    return None


def free_neighbours(
    belief: np.ndarray, row: int, col: int, dy: int, dx: int
) -> Iterator[tuple[int, int]]:
    """Neighbours that are free, inside the map, and not cut across an obstacle corner."""

    ny, nx = row + dy, col + dx
    if not (0 <= ny < belief.shape[0] and 0 <= nx < belief.shape[1]):
        return
    if belief[ny, nx] == WALL:
        return
    if dy and dx and (belief[row + dy, col] == WALL or belief[row, col + dx] == WALL):
        return
    yield ny, nx


@dataclass
class StraightLinePolicy:
    """The behaviour before this branch's fix: nearest frontier in a straight line."""

    agent: Agent
    cooldown_s: float = 0.0
    _failed: list[tuple[Vector3, float]] = field(default_factory=list)

    def select(self) -> Vector3 | None:
        """The nearest frontier cell in a straight line, reachability not considered.

        Frontier cells come off the inflated map, as they did in the loop this branch
        replaced, so the goal at least sits on drivable ground.
        """
        grid = self.agent.costmap().grid
        frontier = (grid == FREE) & _dilate(grid == UNKNOWN)
        cells = np.argwhere(frontier)
        if not len(cells):
            return None
        pose = self.agent.pose
        best = min(cells, key=lambda c: (c[0] - pose.y / RES) ** 2 + (c[1] - pose.x / RES) ** 2)
        return Vector3((best[1] + 0.5) * RES, (best[0] + 0.5) * RES, 0.0)

    def reached(self, goal: Vector3) -> None:
        return None

    def failed(self, goal: Vector3) -> None:
        return None


def _dilate(mask: np.ndarray) -> np.ndarray:
    padded = np.pad(mask, 1)
    out = np.zeros_like(mask, dtype=bool)
    for dy in range(3):
        for dx in range(3):
            out |= padded[dy : dy + mask.shape[0], dx : dx + mask.shape[1]]
    return out


def production_policy(agent: Agent, **config: Any) -> WavefrontFrontierExplorer:
    """The real module, driven by hand the way its exploration loop drives it."""

    module = WavefrontFrontierExplorer()
    module.config = WavefrontConfig(**config)
    return module


def run(
    policy: str,
    world: World,
    limit_s: float = 900.0,
    target: float = 0.92,
    config: dict[str, float] | None = None,
) -> dict[str, float]:
    agent = Agent(world)
    if policy == "straight":
        selector = StraightLinePolicy(agent)
        select, reached, failed = selector.select, selector.reached, selector.failed
    else:
        module = production_policy(agent, **(config or {}))

        def select() -> Vector3 | None:
            return module.select_goal(agent.pose, agent.costmap())

        def reached(goal: Vector3) -> None:
            module.mark_explored_goal(goal)

        def failed(goal: Vector3) -> None:
            module.mark_failed_goal(goal)

    marks: list[float] = []
    stall = 0
    idle = 0
    while agent.time < limit_s and agent.covered() < target:
        goal = select()
        if goal is None:
            break
        if agent.walk(goal):
            reached(goal)
            stall = 0
        else:
            failed(goal)
            stall += 1
            agent.time += 5.0  # the seconds spent trying, then backing off
            if stall > 12:
                break
        marks.append(agent.covered())
        if agent.covered() <= (marks[-2] if len(marks) > 1 else -1.0):
            idle += 1
            if idle > 8:
                break
        else:
            idle = 0
    covered = agent.covered()
    return {
        "coverage": covered,
        "minutes": agent.time / 60.0,
        "metres": agent.distance,
        "rewalk_pct": 100.0 * agent.rewalked / max(agent.distance, 1e-9),
        "failures": float(agent.failures),
        "goals": float(agent.attempts),
    }


def _mean_of(rows: list[dict[str, float]]) -> dict[str, float]:
    """The mean of every numeric column over the seeds' results."""

    return {key: sum(row[key] for row in rows) / len(rows) for key in rows[0]}


SCENES: dict[str, Callable[[int], Scene]] = {
    "office": office,
    "corridor_turn": corridor_turn,
    "stairs": stairs,
}
POLICIES = (
    ("straight line", None),
    ("reachable cost", None),
    ("reachable no cooling", {"failure_cooldown": 0.0}),
    ("reachable no repeats off", {"visited_exclusion_radius": 0.0}),
)


def main(seeds: range = range(1, 6), scenes: tuple[str, ...] = ("office", "corridor_turn")) -> None:
    for scene in scenes:
        _report(seeds, scene)


def _report(seeds: range, scene: str) -> None:
    print(f"\nscene={scene} seeds={list(seeds)} resolution={RES} m lidar={RAYS} rays/{RANGE:g} m\n")
    header = f"{'policy':<22}{'coverage':>9}{'minutes':>9}{'metres':>9}{'re-walk%':>10}{'failed':>8}{'goals':>7}"
    print(header)
    print("-" * len(header))
    for label, config in POLICIES:
        policy = "straight" if label == "straight line" else "reachable"
        rows = []
        for seed in seeds:
            # The stairs_free case stands in for the 2.5D traversability layer: the
            # flight is drivable rather than a wall, so upstairs is worth exploring.
            world = raster(SCENES[scene](seed), stairs_free=policy != "straight")
            rows.append(run(policy, world, config=config))
        mean = _mean_of(rows)
        print(
            f"{label:<22}{mean['coverage']:>9.3f}{mean['minutes']:>9.1f}{mean['metres']:>9.1f}"
            f"{mean['rewalk_pct']:>10.1f}{mean['failures']:>8.1f}{mean['goals']:>7.1f}"
        )


if __name__ == "__main__":
    main()
