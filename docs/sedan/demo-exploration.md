# Demo exploration

`DemoExplorer` adds **autonomous frontier-based exploration** on top of the
persistent stack. It reaches reachable free-space frontiers, navigates to them,
and stops when it has collected enough new information. It replaces the
`WavefrontFrontierExplorer` in the demo blueprint and is registered as
`demo-explorer`.

You can start it from the console (**Exploration → Explore building**) or from
the agent chat ("begin exploration" calls `begin_demo_exploration`), and stop it
with **Stop exploration** or "stop exploring".

> **Experimental.** This is a *local* selection strategy: it does not guarantee
> a globally shortest tour or that every room is reachable. The smaller obstacle
> inflation does **not** mean the robot can safely pass every narrow corridor —
> supervise in person.

## The two strategies

| Strategy | How it scores |
|---|---|
| `frontier` | The existing combined score from frontier size, distance, obstacle distance, and direction — only reachable free-space goals. |
| `efficient` | Scores by boundary gain over **actual traversable free-space path distance** (Dijkstra), not a straight-line "through the wall" estimate. Diagonals cannot cut occupied corners. Reached areas are excluded, but the rest of a long boundary can still be explored. |

### Per-run parameters

Set these in the exploration form (they apply to *this* run and can be re-opened
to edit):

| Parameter | Default | Meaning |
|---|---|---|
| `strategy` | `frontier` | Selection strategy |
| `min_goals` | 10 | After this many **successful** arrivals, start checking for low gain — **not** a total goal cap; failures do not count |
| `gain_percent` | 1 | Map-information growth per successful navigation, in percent (1 = 1%) |
| `no_gain_attempts` | 2 | Report low gain after this many arrivals; low gain alone is not completion |
| `check_interval` | 3 s | Progress-check interval — **not** a hard per-goal timeout; an explicit failure wakes immediately and changes goal |
| `max_leg_distance_m` | 1.5 m | Maximum known-free path length per navigation leg |

A failed area cools for 60 s to avoid repeatedly picking the same unreachable
point. Both demo modes use **10 cm** obstacle inflation (the original Wavefront
is 25 cm); the planner's own clearance checks and on-board avoidance are not
changed.

## Bounded startup scan

When you ask to explore, the start cell may be invalid (occupied or unknown).
`DemoExplorer` handles this:

1. Wait 1 s at rest if the start is still invalid.
2. If still invalid, and **alignment is approved, lidar/odometry/costmap are
   fresh, and the rotation clearance is confirmed**, it turns in place at
   `0.15 rad/s` for at most 8 s.
3. As soon as the start becomes a valid free cell, it stops turning and
   rejoins the original `Explore`.
4. "Stop exploring" cancels both phases.

The clearance check uses a **fixed 0.55 m radius** (it does *not* shrink with the
Demo planner width): no known obstacle inside, the outside must be observed free,
and only the `0.30 m` under the body may be unknown. It does **not** clear
obstacles, fill unknown cells, or bypass Restore alignment. On insufficient
clearance, stale sensors, or a still-invalid start after the sweep, it reports
**`blocked`**, not `completed`. The in-place turn cannot guarantee removing the
under-lidar blind spot — supervise in person; it never drives blind into
unknown space.

## Frontier-distance scheduling

At the start of each run the frontier-distance filter starts at **0.05 m** and
grows linearly with the robot's planar displacement (relative to the run's
odometry origin) since this exploration started:

```text
0.05 + 0.35 × min(max_displacement / 2.0, 1)   metres  →  0.40 m at 2 m
```

- Waiting and arrivals do **not** push the growth; returning to the start does
  not lower the grown threshold.
- This depends on odometry — drift or a coordinate correction can affect the
  displacement estimate; it is **not** an independent real-motion detector.
- The exclusion radius for reached goals grows correspondingly from 0.05 m to
  0.75 m; failed goals are still excluded at 0.75 m and cooled.

During the startup phase, the nearest qualifying goal whose actual path
distance is ≤ 0.8 m is preferred. **0.05 m is a frontier filter threshold, not a
navigation step**: the original navigation arrival tolerance is still 0.20 m, so
a too-close frontier only finds a same-direction goal at least 0.25 m away in
known, reachable free space, with the connector's angle to the frontier ≤ ~32°
and the path ≤ 0.8 m. If it cannot connect, it waits and explains why — it does
not fake a 5 cm move or push into unknown. Without a nearby frontier it selects a farther frontier by the chosen strategy,
then walks only a bounded intermediate leg toward it. After 2 m of displacement
normal frontier ranking resumes, but the short-leg limit remains active.

All goals still pass the original planner's safety checks. Waiting / completion
reasons distinguish non-free start cell, reachable frontier count, post-exclusion
count, too-small frontier clusters, un-extendable short goals, and cooled-goal
count.

### Diagnosing an invalid start cell

The backend emits a structured **`Exploration start cell invalid`** warning at
startup checks and when goal selection cannot reach even its start cell.
Use `dimos log -f --json` to inspect its fields. Existing waiting/blocked status
messages and navigation behavior are unchanged.

`causes` distinguishes observed conditions:

- `raw_start_unknown`: the original map has no free-space observation at the robot.
- `raw_start_occupied` / `raw_start_non_free_cost`: the original cell is already
  occupied or has another nonzero cost.
- `raw_start_outside_map`: the current pose maps outside the grid.
- `obstacle_inflation`: the original cell is free but inflation makes it non-free.
- `failed_goal_exclusion`: the inflated cell is free but a temporary failed-goal
  exclusion covers it.
- `frame_mismatch`: odometry and map frame IDs differ.

Each warning includes `stage`, `robot_xyz`, frame IDs, map origin/yaw, dimensions,
resolution, inflation radius, map revision, and sensor receive ages. Compare
`raw_start`, `inflated_start`, and `routing_start` (cell indices, values, states)
to see where the start becomes invalid. `raw_neighborhood` is a clipped 5-by-5
patch in row/y then column/x order (empty outside the map). `covering_failed_zones`
lists overlapping live exclusions, their target positions, distances and remaining
cooldown seconds.

These are map-based observations, not proof of a physical cause: unknown may be
a lidar blind spot; occupied points may be real obstacles or artifacts; an
out-of-map cell may reflect localization, alignment or map extent. Use the logged
coordinates and map view to distinguish them. High-rate rotation scan checks do
not repeat this warning every 0.1 seconds.

## Stopping and recovery

- If there is no ≥ 15 cm translation progress within 15 s, the current goal is
  cancelled and it **tries to recover** before giving up on that goal: it turns
  in place and, if that makes no progress, backs up, then re-checks the goal.
  Failed recovery cools the goal and chooses another. A goal cannot repeatedly
  recover forever: `stall_recovery_attempts` bounds recovery cycles for that leg.
- If odometry or lidar is stale for > 5 s, it stops and reports.
- The map must exist; while fusion is paused it may continue with a static map.
- The run does **not** stop on a low-gain plateau, visited-goal exclusions, or
  cooling failed targets. Low gain is a progress diagnostic, not completion.
- Rotation recovery requires fresh sensors and observed clearance; back-up also
  checks an inflated, known-free connector. Unsafe recovery reports blocked
  instead of driving blind.
- **Stop exploration** cancels exploration. A PGO failure or a remote stop
  signal also ends it and does **not** auto-resume, to avoid accidental motion.

### Greedy goal selection

By default the run picks the **closest reachable frontier** (shortest
traversable path) rather than a distant detour, so it does not circle over
already-covered ground to reach a far frontier. Set `greedy` off to fall back to
the original frontier / efficient scoring.

### Stable enclosure completion

Completion requires **three distinct map/lidar observations spanning at least
five seconds**, all showing a nonempty robot-reachable free component with no
unknown boundary and no opening onto the map edge. Alignment must be ready and
lidar/odometry fresh. Checks use the uninflated map so obstacle inflation does
not falsely seal a doorway.

This bypasses `min_goals`: a genuinely closed small room can finish even before
the first successful goal. A stale map, unknown hole, transient closure, non-free
start, or only filtered/cooling frontiers does not prove completion. Repeated
polls of one unchanged map do not count as new evidence. While fusion is paused,
new world-frame lidar scans can confirm the static map only if their observed
points remain in its known cells; a scan exposing unknown/out-of-map space
resets closure evidence. Scans must contain at least 32 finite points and have
new timestamps. This avoids requiring the robot to move just to unpause fusion.
The legacy `coverage_threshold` parameter is retained for
compatibility; it cannot override remaining unknown boundaries.

Every leg is limited to **1.5 m of traversable path**, not Euclidean distance.
Distant frontier targets are approached through intermediate known-free
waypoints; diagonals never cut occupied corners. This conservative local
behavior is not a strict indoor boundary detector or a safety geofence.

## Demo planner width & speed

| Setting | Where | Default | Range |
|---|---|---|---|
| **Demo planner width** | Settings → Mapping & tagging | 0.30 m | 0.05–1.00 m |
| **Live navigation speed limit** | chat panel slider | 0.55 m/s | 0.10–0.55 m/s |

- **Demo planner width** only overrides the demo planner's `robot-width`; it
  does not change global defaults, other blueprints, on-board avoidance, or
  rotation clearance. Smaller reduces path inflation and clearance, but below
  the robot's real width (plus payload) **under-estimates** the occupied space
  and risks collision — not a safe-width recommendation, and `0.05 m` is not a
  safe width. It does not raise walking speed and does not change Demo's own
  10 cm obstacle inflation or navigation arrival tolerance.
- **Live navigation speed limit** is a **translation** cap. On release it takes
  effect immediately on subsequent published translation commands — including an
  in-progress precise/nearby navigation or exploration — **without cancelling or
  rebuilding the current goal**. Turning or the original controller /
  `nerf_speed` may make the actual speed lower; it does not change teleop,
  in-place rotation, visual-search turning, or on-board avoidance. A setting
  below the controller's original 0.20 m/s minimum is still clamped at the
  output, not raised. Failure shows an error and the slider falls back. This
  only affects the current run; Settings' **Navigation speed limit** sets the
  saved default for the next start. The live cap does **not** start motion by
  itself, but a higher cap can speed up a robot already navigating — supervise
  and keep the area clear.

The standalone console starts `unitree-go2-agentic-persistent-demo`, and the
embedded console blueprint also uses `DemoExplorer`. The original
`unitree-go2-agentic-persistent` and Wavefront exploration are unchanged.

## Related

- [Quick start: the persistent workflow](quickstart.md)
- [Persistent maps & relocalization](persistent-maps.md)
- [Testing & verification](testing.md)
