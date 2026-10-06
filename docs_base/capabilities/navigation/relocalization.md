# Premap & Relocalization

Relocalization lets a Go2 navigate on a previously built map instead of only on what it sees right now. At runtime, `RelocalizationModule` aligns live LiDAR to a saved premap and publishes a `world → map` transform, so the costmap and planner operate on the live scan and premap together.

![relocalize on the live go2 and nav_to a point in the premap](assets/reloc_and_nav_to.webp)

> **Note:** Requires dimOS v0.0.13 or newer for PGO loop closure and `dimos map` export.

This guide takes four steps:

1. Record a walk-through with `unitree-go2-memory`
2. Build the premap with `dimos map global {DB_NAME} --export`
3. Test relocalization in replay, no robot needed
4. Deploy on the live Go2

Throughout this guide, `{DB_NAME}` is the stem of your recording, for example `recording_go2` for `recording_go2.db`. For `map_file`, pass the same stem and dimOS appends `.pc2.lcm` automatically.

## 1. Record a run

Drive the Go2 through the space you want as your premap. Close loops when you can because PGO uses revisits to correct drift.

```bash
dimos --robot-ip {YOUR_ROBOT_IP} run unitree-go2-memory
```

If `ROBOT_IP` is set in the environment or `.env`, you can omit `--robot-ip`:

```bash
dimos run unitree-go2-memory
```

This writes `recording_go2.db` to the repo root (`DIMOS_PROJECT_ROOT`) and records `lidar`, `odom`, and `color_image` plus the live TF tree. `dimos --record run unitree-go2` records the same streams (and every other one) to `recordings/<run-id>/memory.db` instead; see [Recording](/docs/usage/recording.md). The recorder stamps lidar frames with the latest odom pose so `dimos map global` can reconstruct poses later- see [`Go2Memory`](/dimos/robot/unitree/go2/blueprints/smart/unitree_go2.py).

### Quick validation (optional)

Before building a premap, inspect the recording:

```bash
dimos mem summary recording_go2
dimos map replay recording_go2 --duration 60
```

`summary` prints stream names and time ranges. `replay` opens Rerun so you can confirm lidar and odometry look sane.

## 2. Build the premap

Export a loop-closed global map as `.pc2.lcm`:

```bash
dimos map global recording_go2 --export
```

| Flag | Effect |
|------|--------|
| `--export` | Run PGO and write `./{DB_NAME}.pc2.lcm` to the current working directory (implies `--pgo`) |
| `--no-gui` | Skip launching Rerun for headless servers or CI |
| `--pgo-tol 0.3` | Spatial dedup tolerance for keyframes in meters. Use `0` to keep all posed frames |
| `--voxel 0.05` | Voxel size in meters (default matches live mapper) |

`{DB_NAME}` accepts a bare stem, `./path/to/file.db`, or an absolute path. Bare names resolve in this order:

1. Current working directory
2. `DIMOS_PROJECT_ROOT`
3. `data/` via LFS (`get_data`)

Examples:

```bash
dimos map global recording_go2 --export --no-gui
dimos map global ./recordings/office_walk.db --export
dimos map global data/go2_hongkong_office.db --export
```

Sample log:

```
running PGO twopass map...
  Pass 1: 908 frames, 1 keyframes
exporting PGO twopass map to .../recording_go2.pc2.lcm...
wrote .../recording_go2.pc2.lcm
```

Open the companion `{DB_NAME}.rrd` in Rerun to verify loop closure before deploying to hardware.

## 3. Relocalize in replay

Test alignment without the robot. `unitree-go2-relocalization` is `unitree-go2` plus `RelocalizationModule`:

```bash
dimos --replay --replay-db recording_go2 run unitree-go2-relocalization \
  --map-file=recording_go2
```

`map_file` resolves `{DB_NAME}.pc2.lcm` with the same search order as above (cwd, then project root, then `data/`).

### Reading the logs

```
Relocalization module started: map_file='recording_go2'  loaded_map.frame_id='map'
relocalize skipped: n_pts=37770 < MIN_LOCAL_POINTS=50000
relocalize rejected: fitness=0.433 < threshold=0.45 time_cost=8.1s n_pts=57385
relocalize: fitness=0.657 time_cost=3.0s n_pts=64703 reloc_t=[-0.007, -0.01, -0.102] TF 'world' -> 'map' published_t=[0.007, 0.009, 0.102]
```

`relocalize skipped` means the live submap is still warming up- fewer than `MIN_LOCAL_POINTS` points accumulated. `relocalize rejected` means a candidate alignment was found but its fitness was below the threshold, so no transform is published. Once `relocalize:` lines appear at info level, the `world → map` TF is live.

You can replay a different `.db` from the same physical space against the same premap to test generalization.

### Rerun visualization

Watch alignment in Rerun, which is enabled by default on Go2 blueprints:

- **Merged map** shows the premap transformed into `world` plus the live scan, column-carved together.
- Toggle the merged map entity off to compare the live scan alone against the merged costmap.

## 4. Relocalize on a live robot

Run the replay test first. On hardware, use the same blueprint and `map_file`:

```bash
dimos --robot-ip {YOUR_ROBOT_IP} run unitree-go2-relocalization \
  --map-file=recording_go2
```

Before sending navigation goals, walk through this checklist:

1. Place the Go2 in a region that overlaps the premap on the same floor with recognizable geometry.
2. Wait for `relocalize:` info lines. Skipped and rejected lines are normal for the first 30 to 60 seconds.
3. Confirm stable `world → map` TF in Rerun before sending navigation goals.
4. Click to navigate or use agent skills such as `navigate_with_text` on the aligned costmap.

## How it works

The `unitree-go2-relocalization` blueprint is the standard [Go2 navigation stack](/docs/capabilities/navigation/deep_dive.md) plus `RelocalizationModule`:

<details>
<summary>diagram source</summary>

```python skip fold output=assets/go2_reloc_blueprint.svg
from dimos.core.coordination.blueprints import autoconnect
from dimos.core.introspection.svg import to_svg
from dimos.mapping.relocalization.module import RelocalizationModule
from dimos.robot.unitree.go2.blueprints.smart.unitree_go2 import unitree_go2

unitree_go2_relocalization = autoconnect(
    unitree_go2,
    RelocalizationModule.blueprint(),
).global_config(n_workers=11)

to_svg(unitree_go2_relocalization, "assets/go2_reloc_blueprint.svg")
```

</details>

![unitree-go2-relocalization blueprint module graph](assets/go2_reloc_blueprint.svg)

Note that [`CostMapper`](/dimos/mapping/costmapper.py) builds the costmap from the merged map only while [`RelocalizationModule`](/dimos/mapping/relocalization/module.py) has a good alignment; until then it falls back to the live map alone.

### File formats

| File | Format | Produced by | Consumed by |
|------|--------|-------------|-------------|
| `{name}.db` | memory SQLite (`lidar`, `odom`, `color_image`, …) | `unitree-go2-memory` | `dimos map *`, `--replay-db` |
| `{name}.pc2.lcm` | LCM-encoded `PointCloud2` premap | `dimos map global --export` | `RelocalizationModule` (`map_file`) |
| `{name}.rrd` | Rerun recording (visual QA) | `dimos map global` | Rerun viewer |

## Configuration reference

CLI overrides use dynamically generated kebab-case flags such as
`--map-file=…`. If a shorthand is ambiguous, qualify it with the module key,
for example `--relocalizationmodule.map-file=…`.

| Field | Default | Description |
|-------|---------|-------------|
| `map_file` | `None` (module disabled) | Premap stem or path. dimOS appends `.pc2.lcm` automatically |
| `fitness_threshold` | `0.45` | Minimum ICP fitness to accept a relocalization (0 to 1) |
| `tf_interval` | `10.0` | Seconds between tf republishes of the accepted fix (published immediately on every fix) |
| `republish_loaded_map` | `0.0` | Seconds between `loaded_map` republishes once placed; `0` publishes once per fix |
| `use_carving` | `true` | Column-carve when merging premap and live scan |

Constants are not overridable via CLI today:

| Constant | Value | Role |
|----------|-------|------|
| `MIN_LOCAL_POINTS` | `50_000` | Minimum live map points before attempting relocalization |
| `RELOC_INTERVAL` | `2.0` s | Throttle between relocalization attempts |
| `PUBLISH_INTERVAL` | `2.0` s | TF publish rate |

To accept all candidates for visualization only (not for production nav):

```bash
dimos run unitree-go2-relocalization \
  --map-file=recording_go2 \
  --fitness-threshold=0.0
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `Relocalization module disabled (no map_file configured)` | Missing `--map-file=…` | Set `map_file` to your premap stem |
| File not found for `.pc2.lcm` | Export not run or wrong cwd | Run `dimos map global … --export` and check cwd or `data/` |
| Long stretch of `relocalize skipped` | Map still accumulating points | Wait or drive slowly through mapped geometry |
| Repeated `relocalize rejected` | Poor overlap with premap or wrong space | Start in a known area and check premap in `.rrd` |
| Nav works but map looks misaligned | Low fitness accepted in debug mode | Raise `fitness_threshold` back to default `0.45` |
| PGO map looks wrong | Bad odometry in recording | Run `dimos map replay` or `summary` and re-record with smoother motion |

## Supervised persistent maps with the Go2 agent

`unitree-go2-agentic-persistent` is an opt-in variant of the agentic stack.
It saves a live column-carving map, restores it on the next run, and transforms
new lidar, odometry, and root TF into the **original session's world frame**.
The planner and SpatialMemory therefore use the same stable coordinates.
The original `unitree-go2-agentic` blueprint is unchanged.

Matching with Go2's built-in lidar is **experimental**: the existing alignment
preset was measured on MID360, not this sensor. A candidate passing the
algorithm's threshold is not proof of a correct match. This variant requires
human approval on every restored session; approval is an RPC, not an LLM tool.
It does not add loop closure or eliminate subsequent odometry drift.

### First run: create a map

Use a new scene-memory directory for the first persistent session. Previously
collected scene memories may belong to a different odometry origin; a `.pc2.lcm`
map must be collected in the same coordinate system as its semantic memory.
An old ChromaDB or VLM report alone cannot reconstruct the missing lidar map.

```bash
dimos run unitree-go2-agentic-persistent \
  --robot-ip "$ROBOT_IP" \
  --persistentgo2map.map-file=assets/scene_maps/sedan_office_persistent/map.pc2.lcm \
  --persistentgo2map.create-new=true \
  --spatialmemory.scene-map-dir=assets/scene_maps/sedan_office_persistent \
  --mcpclient.model=gpt-4o-mini
```

Add your usual `--spatialmemory.vlm-*` flags to annotate new frames if needed.
The first session defines the persistent coordinates; no alignment approval is
needed for a new map. `create_new` refuses to replace an existing file.
The map saves every 30 seconds and on graceful shutdown (`Ctrl+C` or
`dimos stop`). A force kill can lose changes since the last save.

For an explicit save, open `dimos shell` in another terminal:

```python
app.PersistentGo2Map.save_map()
```

Saving before any accepted scan is an error. Save the map and stop normally
before returning to the space.

### Next run: restore, inspect, and approve

Use the same paths, **without** `create_new`:

```bash
dimos run unitree-go2-agentic-persistent \
  --robot-ip "$ROBOT_IP" \
  --persistentgo2map.map-file=assets/scene_maps/sedan_office_persistent/map.pc2.lcm \
  --spatialmemory.scene-map-dir=assets/scene_maps/sedan_office_persistent \
  --mcpclient.model=gpt-4o-mini
```

A missing or invalid map fails startup rather than silently creating a new one.
Before approval, aligned sensor forwarding, planner goals, semantic observations,
and map saves are blocked. Existing low-level/manual robot controls are not
disabled: do not ask the agent to move while checking alignment.

Place the robot in a distinctive overlapping area. In Rerun compare:

- `world/alignment_scan`: live lidar in this session's coordinates.
- `world/alignment_preview`: the saved map placed into those coordinates by
  the candidate. Walls, corners, and floor should agree, not just one surface.

#### Optional startup scan rotation

For a restored map, add these flags to the restore command:

```bash
--persistentgo2map.startup-rotation=true \
--persistentgo2map.rotation-speed=0.15 \
--persistentgo2map.rotation-duration=20.0
```

This opt-in capture starts after fresh lidar and odometry arrive, commands only
yaw (zero translation), and turns for at most 20 seconds (about 172 degrees
at the requested rate, not a measured angle). It retains a voxelized union of
the whole sweep, rather than only the last 3–7 scans. The robot receives a zero
velocity command before matching begins. Human alignment approval is still
required. New-map sessions never run this rotation.

**Clear the robot's entire turning footprint and supervise it.** This capture
does not perform obstacle avoidance. It stops on stale lidar/odometry (default
1 second), module shutdown, a stop-movement message, or another nonzero velocity
command routed through this blueprint. Direct robot RPC controls bypass that
routing; do not issue agent movement commands during capture.

To cancel from `dimos shell`:

```python
app.PersistentGo2Map.cancel_startup_rotation()
```

Cancellation or sensor loss leaves alignment blocked and never restarts rotation
automatically; stop and restart to retry. Rejecting a completed candidate retries
matching the captured sweep without another rotation. Leave `startup-rotation`
disabled for manual scan acquisition. Rotation improves coverage but cannot
guarantee a correct match in repetitive geometry or fix odometry drift.

#### Manual walking and turning capture

To collect scans while you remotely drive the robot, use
`--persistentgo2map.manual-capture=true` instead of `startup-rotation=true`.
The two options cannot be enabled together. This mode commands no automatic
motion and keeps the voxelized union of all scans until you finish capture.
It only applies to restored maps; new-map sessions are unchanged.

Drive slowly through a clear, overlapping part of the saved map and turn to
observe different walls and corners. Supervise the robot: this mode adds no
obstacle avoidance. Navigation and map updates remain blocked.

**Release your remote controls and stop the robot first**, then in `dimos shell`:

```python
app.PersistentGo2Map.alignment_status()  # Captured scan and point counts
app.PersistentGo2Map.finish_startup_capture()
```

Finishing also publishes a zero velocity command, but cannot override a held
physical remote control. Too few points produces an error and leaves capture
active so you can continue. Matching starts only after finishing; a rejected
candidate retries the same captured cloud without moving the robot. Restart
to collect a different sweep if needed.

Open `dimos shell` and inspect:

```python
app.PersistentGo2Map.alignment_status()
```

If the match is correct:

```python
app.PersistentGo2Map.confirm_alignment()
```

If it is wrong:

```python
app.PersistentGo2Map.reject_alignment()
```

Rejecting leaves navigation blocked and resumes matching. If matching repeatedly
fails, stop and collect a recording for tuning; do not reduce the threshold to
force a placement. Approval clears the preview entities and publishes the old
map on `world/global_map`. New observations replace seen voxel columns while
unobserved parts of the old map remain, and saves retain the original coordinates.
To undo an approved placement, stop and restart; do not change coordinates
mid-navigation.

After approval, send commands from another terminal:

```bash
dimos agent-send "List the remembered locations without moving."
dimos agent-send "Navigate to office."
```

The requested location must exist in the paired semantic memory. Check alignment
in Rerun and ensure the path is clear before requesting physical navigation.

### Query stored tags and navigate to a selected tag

The agent exposes two additional MCP tools:

- `query_memory_tags(query="")`: read-only inventory of persisted tags, including
  IDs, names, estimated world coordinates, category, description, and counts.
  An empty query returns all tags; a nonempty query is a case-insensitive name
  substring filter. Use the stored names, not translated names.
- `navigate_to_memory_tag(location_id=...)`: load the selected tag from memory
  and submit its horizontal coordinates to the existing planner. The goal uses
  current odometry height, not object height. Alignment approval remains required.
  If multiple tags match, the agent should ask which one unless the user supplied
  a selection criterion. This tool starts navigation; it does not wait for arrival.

```bash
dimos agent-send "List the objects tagged in memory and their coordinates. Do not move."
dimos agent-send "How many fire extinguisher tags are stored? List each ID and position."
dimos agent-send "Navigate to the fire extinguisher."

dimos mcp call query_memory_tags --json-args '{"query":"fire extinguisher"}'
dimos mcp call navigate_to_memory_tag --json-args '{"location_id":"<ID from query>"}'
```

Restart the stack after updating code so the agent fetches the new tools.
Queries do not trigger navigation and include tags from previous sessions.
Counts describe stored tags, not a verified count of physical objects. New VLM
object tags with the same name and estimated positions within 1 metre are merged;
more distant observations are kept as separate tags. Older tags are retained and
may have unknown category. Previously discarded same-name objects cannot be
recovered without observing them again. Estimates can be wrong or stale; inspect
the map before navigating. The planner may choose a reachable point near an object
rather than its occupied position.

### Tag the object, not the robot's observation location

Use `tag_object` for a visible object:

```bash
dimos agent-send "Use tag_object to mark the visible fire extinguisher. Do not move."
dimos mcp call tag_object --json-args '{"object_name":"fire extinguisher"}'
```

`tag_location` intentionally records the **robot's current location**; it is for
named waypoints, not physical objects. `tag_object` detects an image box, refines
it with the configured segmenter when available, projects time-aligned lidar
points through calibrated camera intrinsics into that region, and transforms
the foreground surface estimate into `world`. Geometry is captured before model
inference, so subsequent robot motion does not change the observation's frame.
Automatic VLM object tags use the same depth requirement.
Room/place tags instead use the robot's position when the room was observed,
even if a place bounding box is provided. They represent a return waypoint in
the room, not its geometric centre or the position of a wall.

No valid lidar hit means an explicit failure (or a logged skipped automatic tag),
not a robot-position or fixed-distance fallback. Camera calibration, camera TF,
and lidar/image time alignment must be available after map alignment approval.
This is a lidar-supported surface estimate, not a guaranteed object centre.
When instance segmentation is unavailable, bounding-box depth is less selective
and can include background; inspect the result. Existing incorrectly placed tags
are not automatically deleted or repaired.

## Related docs

For hardware setup, simulation, and the full blueprint list, see the [Go2 platform guide](/docs/platforms/quadruped/go2/index.md). The [v0.0.13 release notes](https://github.com/dimensionalOS/dimos/releases/tag/v0.0.13) summarize the PGO, `dimos map`, and relocalization work this guide builds on.
