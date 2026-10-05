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
| `no_gain_attempts` | 2 | End after this many consecutive low-gain attempts |
| `check_interval` | 3 s | Progress-check interval — **not** a hard per-goal timeout; an explicit failure wakes immediately and changes goal |

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
not fake a 5 cm move or push into unknown. Without a near goal it selects a far
goal by the chosen strategy; after 2 m of displacement it reverts to normal
scoring.

All goals still pass the original planner's safety checks. Waiting / completion
reasons distinguish non-free start cell, reachable frontier count, post-exclusion
count, too-small frontier clusters, un-extendable short goals, and cooled-goal
count.

## Stopping

- If there is no ≥ 15 cm translation progress within 15 s, the current goal is
  cancelled and it picks another.
- If odometry is stale for > 5 s, it stops and reports.
- The map must exist; while fusion is paused it may continue with a static map.
- Ten consecutive attempts without a qualifying goal end it and show the reason.
- A larger restored map may trigger low gain quickly — lower `gain_percent` or
  raise `min_goals` / `no_gain_attempts`.
- **Stop exploration** cancels exploration. A PGO failure or a remote stop
  signal also ends it and does **not** auto-resume, to avoid accidental motion.

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
