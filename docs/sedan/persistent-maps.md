# Persistent maps & relocalization

`unitree-go2-agentic-persistent` is an **opt-in** variant of the agentic stack.
It saves a live column-carving map, restores it on the next run, and transforms
new lidar, odometry, and root TF into the **original session's world frame**, so
the planner and the spatial memory use one stable coordinate system. The
original `unitree-go2-agentic` blueprint is unchanged.

> **Experimental.** Matching with the Go2's *built-in* LiDAR is experimental:
> the existing alignment preset was measured on a MID360, not this sensor. A
> candidate that passes the algorithm's threshold is **not** proof of a correct
> match. This variant requires **human approval on every restored session** —
> approval is an RPC, not an LLM tool. It does **not** add loop closure or
> remove subsequent odometry drift.

The two modules are:

- `PersistentGo2Map` — gates live sensors until a human approves their placement
  in a saved map, and owns save / restore / alignment / fusion.
- `PersistentGo2Planner` — refuses navigation goals before the saved coordinate
  system is approved, and adds nearby + visual arrival.

Both are registered in `all_blueprints.py` as `persistent-go2-map` and
`persistent-go2-planner`.

## Lifecycle at a glance

| Phase | Who | What happens |
|---|---|---|
| **Create** | `PersistentGo2Map` | A new scene defines the world frame; no alignment needed. The map autosaves every 30 s and on graceful stop. |
| **Restore** | `PersistentGo2Map` | Load the map, capture scans, match, and **block** navigation / aligned sensor forwarding / map updates until a human approves alignment. |
| **Approve** | human (`confirm_alignment`) | Clears the preview, publishes the old map on `world/global_map`, and unblocks. |
| **Act** | planner + agent | Query tags, navigate (precise / nearby / visual), tag objects/places, explore. |
| **Stabilize** | `FusionMotionGate` | Pause permanent-map growth while the robot is stationary. |
| **Correct** (opt-in) | `PGOMap` | Pose-graph-optimize the current run's map, tags, and pose together. |

## The map file

| File | Format | Produced by | Consumed by |
|---|---|---|---|
| `map.pc2.lcm` | LCM-encoded `PointCloud2` map | `PersistentGo2Map.save_map()` | `PersistentGo2Map` (restore, `map_file`) |
| `chromadb_data/` | SQLite + vector store | `SpatialMemory` | `SpatialMemory`, agent queries |
| `vlm_tags_*.jsonl` | diagnostic record of VLM estimates at capture time | VLM tagging | human inspection (not rewritten by PGO) |

A `.pc2.lcm` map **must** be collected in the same coordinate system as its
semantic memory. An old ChromaDB or VLM report alone cannot reconstruct the
missing lidar map. Save the map and stop normally before leaving the space; a
force-kill can lose changes since the last save.

> Saving before any accepted scan is an error. A paused fusion does **not**
> mean the map grew — check the fusion status and accepted count.

## Alignment (the human gate)

On a restored map, alignment runs in these stages:

1. **Capture** — manual (`--persistentgo2map.manual-capture=true`) or startup
   rotation (`--persistentgo2map.startup-rotation=true` …). The two cannot be
   enabled together. Rotation starts after fresh lidar/odom, commands **yaw
   only** (zero translation), and turns for at most the configured duration.
   It retains a voxelized **union** of the whole sweep, not just the last few
   scans. **Clear the robot's entire turning footprint and supervise it** —
   this capture does no obstacle avoidance.
2. **Match** — `finish_startup_capture()` freezes the cloud and matching
   begins. Too few points errors and keeps capture active.
3. **Inspect** — `alignment_status()` and the Rerun `world/alignment_scan`
   (captured) vs `world/alignment_preview` (placed map). Check walls, corners,
   floor, position, **and heading**.
4. **Decide** — `confirm_alignment()` / `reject_alignment()`, **human-only**.
   Rejecting retries the same frozen cloud without moving the robot; to
   re-capture, stop and restart. An approved placement cannot be undone online.

```python
# in dimos shell
app.PersistentGo2Map.alignment_status()     # readiness + candidate transform
app.PersistentGo2Map.confirm_alignment()     # approve
app.PersistentGo2Map.navigation_ready()      # True after approval
app.PersistentGo2Map.reject_alignment()      # discard + resume matching
app.PersistentGo2Map.cancel_startup_rotation()
app.PersistentGo2Map.finish_startup_capture()
```

> Alignment confirmation is **only** an RPC — it is deliberately **not**
> exposed as an agent skill.

### Startup rotation (optional)

```bash
--persistentgo2map.startup-rotation=true \
--persistentgo2map.rotation-speed=0.15 \
--persistentgo2map.rotation-duration=20.0
```

- Rotation only applies to **restored** maps; new-map sessions never rotate.
- It commands a zero velocity before matching begins and stops on stale
  lidar/odometry (default 1 s), module shutdown, a stop-movement message, or
  another nonzero velocity routed through this blueprint. **Direct robot RPC
  controls bypass that routing** — do not issue agent movement commands during
  capture.
- Cancellation or sensor loss leaves alignment blocked and **never** retries
  rotation automatically; stop and restart to retry. A rejected completed
  candidate retries matching the captured sweep without another rotation.
- Rotation improves coverage but **cannot guarantee** a correct match in
  repetitive geometry or fix odometry drift.

### Manual walking & turning capture

Use `--persistentgo2map.manual-capture=true` instead of `startup-rotation=true`
(they are mutually exclusive). This mode commands **no** automatic motion and
keeps the voxelized union of all scans until you finish capture.

```bash
dimos shell
# Python:
app.PersistentGo2Map.alignment_status()          # captured scan + point counts
app.PersistentGo2Map.finish_startup_capture()    # freeze + begin matching
```

Release the remote and stop the robot **first**, then finish. Finishing
publishes a zero velocity but cannot override a held physical remote.

## Fusion gate

Permanent-map growth is gated so maps do not thicken or drift while the robot is
stationary. This is **not** localization correction and does **not** guarantee
a correct pose; if the pose clearly drifts, stop navigation and check
localization rather than masking the problem with the map gate.

| Behaviour | Detail |
|---|---|
| **Auto-pause** | The standalone console enables "auto-pause permanent map on low-speed odometry" by default (using the existing Go2 WebRTC odometry — no extra `point_lio_unilidar`). It only controls *aligned permanent-map* writes; live cloud, odom, and TF keep updating. Non-console CLI/blueprints are off by default; enable with `--persistentgo2map.auto-pause-fusion=true`. |
| **Pause / resume** | `pause_fusion()` is a manual lock that will not auto-release. `resume_fusion()` releases the manual pause only; if the automatic rule still judges stationary / insufficient data, the map stays paused. |
| **Fusion status** | `fusion_status()` reports permanent-map fusion independently of sensor forwarding and navigation (reason, accepted/skipped frame counts). |

Default auto-gate parameters (**not** calibrated on the current robot — tune to
your stationary noise and minimum real speed):

| Parameter | Default |
|---|---|
| Motion window | 0.5 s |
| Stationary dwell | 1 s |
| Odometry timeout | 1 s (must be ≥ the window) |
| Stationary / resume speed | 0.02 / 0.04 m/s |
| Stationary / resume rotation | 2 / 3 deg/s |

- The resume threshold must be higher than the stationary threshold to avoid
  oscillation. Judgement uses a short window, not accumulated displacement since
  the last fusion position, so slow drift does not periodically re-enable
  fusion.
- Before motion is decided, on stale/invalid odom, or on an error, writing is
  paused and the reason is shown; a fresh valid odom allows the first aligned
  scan as a map seed.
- **Save map can save the current fused map but does not pause later fusion**;
  restore is not read-only either. Autosave and clean-stop saving still apply.
- **Pause is not Save map, and not an emergency stop.** To keep the current
  map, pause then save. A polluted map is not auto-repaired — choose a clean
  backup or rebuild a new scene.

**Manual acceptance at rest:** observe the stationary state for ~60 s; after
auto-pause the accepted-frame count should stop growing, saving should not
continue to grow the map, and skipped + live sensor streams keep updating.
Check that normal translation and in-place turning resume, and that stopping
pauses again.

## PGO loop closure (optional)

Enable in the console under **Settings → Mapping & tagging → PGO loop
correction**, or with `--persistentgo2map.pgo-enabled=true` on a direct start.
It is **off by default** and does not change the original mapping method. It
requires GTSAM in the current Python environment; a missing dependency errors
out rather than silently falling back. Back up the whole scene directory before
first use.

What it does:

- `PGOMap` runs ICP loop detection and pose-graph optimization over the
  **current run's** keyframes. The first keyframe is fixed to the aligned world
  frame; later clouds, robot pose, and TF are all corrected.
- New room/object tags and image-retrieval coordinates from this run are
  updated by capture time and original pose; slow-model tags are corrected at
  save without re-applying the same correction twice.
- **Restore mode** keeps the previously saved cloud and old tags; it does not
  drag them by the current loop. New, corrected clouds are saved alongside the
  old map, and the starting location stays in this run's world frame.
- On an accepted loop, the robot briefly stops, syncs the map and tags, saves
  the map, and re-plans the current navigation. Tag navigation re-queries the
  **corrected** coordinates by the original tag ID (not by name, not on the old
  path); directly specified world-coordinate goals (including the starting
  location) stay fixed. The log shows `Navigation resumed after PGO` — that is
  **not** arrival. A user cancel, remote takeover, a finished goal, or a new
  goal submitted during the pause does not resume the old navigation; a new
  goal during the pause is clearly rejected and can be resubmitted after sync.
- The map is saved after the loop; ordinary `Save map` and clean stop also save
  the corrected map. If tag sync, the loop checkpoint, or navigation refresh
  fails, motion stops, navigation and later map saves are blocked, and the log
  gives a specific error — fix it, then restart and re-check alignment.

**PGO is not continuous global relocalization.** It only detects loops within
the current run; on reconnect you still capture, inspect, and approve startup
alignment. A wrong startup alignment or a wrong ICP loop is **not** guaranteed
to be auto-fixed — first acceptance should be in a clear, safe, supervised
area.

Loops are not periodic: a keyframe is added on > 0.5 m translation or > 45°
rotation; after ≥ 10 keyframes it searches within 2 m of a historical frame
more than 20 s earlier; at least 5 s between accepted loops (by sensor time),
each still requiring an ICP pass. The demo applies the correction right after
acceptance — it is not a correction every 20 s.

The map file uses atomic replacement, but the map and the Chroma database are
**not** a cross-file transaction. Do not force-kill or lose power during a loop
sync; afterwards check that the map and tags are consistent and, if needed,
restore the full scene backup. `vlm_tags_*.jsonl` is a diagnostic record of the
estimate at capture time and is not rewritten by a later loop; agent queries and
navigation use the corrected database coordinates.

## Querying tags & navigating

| Tool | Behaviour |
|---|---|
| `query_memory_tags(query="")` | Read-only inventory of persisted tags (IDs, names, estimated world coords, category, description, counts). Empty returns all; non-empty is a case-insensitive name substring. |
| `navigate_to_memory_tag(location_id=...)` | Load the selected tag and submit its horizontal coordinates to the planner. Uses current odometry height, not object height. Alignment approval remains required. |
| `navigate_near_memory_tag(location_id)` | Stop when *near* the tag (within the nearby threshold), not exactly at it — see [movement & arrival](movement-and-arrival.md). |
| `navigate_with_text` | An existing path that tries tags, visible objects, and semantic image memory in turn. |

> **The count is stored tags, not a verified count of physical objects.**
> Estimates can be wrong or stale; inspect the map before navigating. The
> planner may choose a reachable point near an object rather than its occupied
> position.

## Tagging object vs. location

Use `tag_object` for a visible object and `tag_location` for a **return
waypoint**. `tag_object` detects a box, refines it with the configured
segmenter when available, projects time-aligned lidar points through calibrated
intrinsics into that region, and transforms the foreground surface estimate into
`world` — geometry is captured **before** model inference. A place tag instead
uses the robot's position when the room was observed, even if a bounding box is
provided — it is a return waypoint, not the room's centre or a wall position.

A **no valid lidar hit** is an explicit failure (or a logged skipped automatic
tag), never a robot-position or fixed-distance fallback. Camera calibration,
camera TF, and lidar/image time alignment must be available after alignment
approval. When instance segmentation is unavailable, bounding-box depth is less
selective and can include background. Existing wrong tags are not auto-deleted
or repaired.

## Related

- [Quick start: the persistent workflow](quickstart.md)
- [The web console](web-console.md)
- [Nearby & visual arrival](movement-and-arrival.md)
- [Testing & verification](testing.md)
