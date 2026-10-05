# Quick start: the persistent workflow

This guide walks the full lifecycle of `unitree-go2-agentic-persistent` (and
its console variants) end to end: **create** a persistent map, **restore** it,
**align** to it with a human in the loop, **query** remembered tags,
**navigate** (precisely or *nearby*), **tag** objects and places, and
**save**. It is the English companion to the original
[Chinese workflow guide](../usage/go2-persistent-workflow.zh.md) and
[relocalization docs](../capabilities/navigation/relocalization.md).

The map and semantic memory live in a *scene directory*:

```text
assets/scene_maps/sedan_office_persistent
```

## The two lifecycles

**First run (create):**

```text
create new map → drive slowly to map → auto/manual tagging → query to check → save → stop cleanly
```

**Every later run (restore):**

```text
load old map → capture scans → stop the robot → finish capture → inspect alignment
    → human confirms → query / chat / tag / navigate → save → stop cleanly
```

> **Safety.** The commands below use `--no-obstacle-avoidance`, which turns off
> the Go2's on-board obstacle avoidance. Manual capture adds no avoidance of
> its own — drive slowly, keep the robot's footprint clear, and supervise it.
> This flag does *not* disable the DimOS planner's own obstacle handling. In an
> emergency use the physical remote or a hardware stop; do not wait for the LLM
> to answer.

## 1. Set up the environment

In every terminal, work in the project directory with the same Python
environment:

```bash
cd /path/to/dimos
source .venv/bin/activate          # or your venv-dev
export ROBOT_IP="192.168.123.161"
mkdir -p assets/scene_maps/sedan_office_persistent
```

Make sure the OpenAI (or other model) credentials are set through the project
config or environment. **Never commit the secret or paste it into chat.** We
recommend splitting work across terminals:

| Terminal | Purpose |
|---|---|
| A | Start and run the robot stack |
| B | `dimos agentspy` — watch agent messages live |
| C | `dimos shell` — capture, alignment, save, status |
| D | `dimos agent-send`, MCP calls, and logs |

> `OPENAI_API_KEY` and `UNITREE_AES_128_KEY` are read from `.env` in the
> project root (the console reads them too, but never displays them). Keep
> `.env` private and out of version control.

## 2. Create the map (first run)

Use a **fresh** scene directory for the first persistent session. A previously
collected scene memory may belong to a different odometry origin; a
`.pc2.lcm` map must be collected in the same coordinate system as its
semantic memory.

```bash
dimos run unitree-go2-agentic-persistent \
  --robot-ip "$ROBOT_IP" \
  --persistentgo2map.map-file=assets/scene_maps/sedan_office_persistent/map.pc2.lcm \
  --persistentgo2map.create-new=true \
  --spatialmemory.scene-map-dir=assets/scene_maps/sedan_office_persistent \
  --mcpclient.model=gpt-5.6-luna \
  --no-obstacle-avoidance
```

Two model parameters are independent:

| Flag | Use |
|---|---|
| `--spatialmemory.vlm-model` | Analyzes images and produces automatic tags |
| `--mcpclient.model` | Handles chat and chooses agent skills |

Notes for the first run:

- `create-new=true` is for the *first* creation only; it **refuses** to
  overwrite an existing map file.
- A new map needs **no alignment** — this session defines the world
  coordinates that are saved.
- Drive slowly through walls, corners, rooms, and corridors.
- The map autosaves every 30 seconds and on graceful stop.
- Do **not** mix an old semantic directory from a different odometry origin.

For an explicit save, from another terminal:

```bash
dimos shell
# Python:
app.PersistentGo2Map.save_map()
```

> Saving before any accepted scan is an error. Save and stop **normally**
> before leaving the space. A force-kill can lose changes since the last
> save.

### Optional: automatic tagging

Add these flags to enable automatic room and object tagging:

```bash
  --spatialmemory.vlm-enable-place-tagging=true \
  --spatialmemory.vlm-enable-object-tagging=true \
  --spatialmemory.vlm-distance-m=1.0 \
  --spatialmemory.object-segmenter=yolo
```

| Tag | Coordinate source |
|---|---|
| room / place | the robot's position when the room was recognized (a return point) |
| object | the point-cloud-derived position inside the detection region |

The object pipeline is: *detection box → optional segmentation mask → camera
intrinsics + time-aligned TF → time-aligned point-cloud projection →
foreground surface → world coordinates*. If there is no valid point-cloud
depth, the automatic object tag is **skipped and logged** — it never falls
back to the robot's position or a fixed distance.

- `vlm-distance-m=1.0` is a *travel-distance* trigger, not "call the model
  every second".
- YOLO does not guarantee a valid instance mask for every object; a bounding
  box alone can include background.
- What is saved is a *surface estimate*, not a guaranteed geometric centre.
- Same-named objects whose estimates are within 1 m are merged; more distant
  observations are kept separate.

## 3. Query, check, and navigate

Status and logs:

```bash
dimos status                 # stack status
dimos mcp status             # MCP server status
dimos mcp modules            # module → skill mapping
dimos mcp list-tools         # full tool names + argument schemas
dimos log -n 100             # last 100 lines
dimos log -f                 # follow the log
dimos agentspy               # watch agent messages live (terminal B)
```

> After you update code, **restart** the stack. Running workers do not reload
> new tools.

### Query remembered tags

```bash
dimos mcp call query_memory_tags                      # all tags
dimos mcp call query_memory_tags \
  --json-args '{"query":"fire extinguisher"}'         # substring filter
```

This returns each tag's ID, name, world coordinates, category, description, and
match count. Queries read from the *persisted* tag database, so they include
tags from earlier sessions.

- The filter is a **case-insensitive substring** on the stored name — it is
  *not* a translation. Use the name you actually saved.
- **The count is the number of stored tags, not a verified count of physical
  objects.** Older records may lack a category; a second same-named object
  that was dropped by dedup can only be recovered by observing it again.

### Navigate to a tag

From the agent (it will query first, then navigate by ID):

```bash
dimos agent-send "Navigate to the fire extinguisher in memory."
dimos agent-send "Navigate to the tagged office."
```

Direct MCP — query, then navigate by the real ID:

```bash
dimos mcp call query_memory_tags --json-args '{"query":"fire extinguisher"}'
dimos mcp call navigate_to_memory_tag --json-args '{"location_id":"loc_XXXXXXXX"}'
```

`navigate_to_memory_tag` loads the tag's coordinates and submits them to the
existing planner. It uses the goal's x/y with the **current robot height**
(not the object's height), and still goes through the original planner and
map-alignment gate. "Started navigating" is the *start*, not arrival.

- **Arrival** is roughly: target distance `< 0.2 m` and heading error `< 15°`.
  The planner may pick a reachable point *near* an object rather than the
  occupied position.
- **Cancel:** `dimos mcp call stop_navigation`, or in `dimos shell`:

  ```python
  app.PersistentGo2Planner.cancel_goal()
  app.PersistentGo2Planner.is_goal_reached()
  ```

  `is_goal_reached()` returning `False` may also mean the goal was *cancelled*.

## 4. Tag objects and places yourself

**Tag an object** (make the robot see it clearly; confirm alignment first on a
restored map):

```bash
dimos agent-send "Use tag_object to mark the fire extinguisher in front of you. Name it fire extinguisher. Do not move."
dimos mcp call tag_object --json-args '{"object_name":"fire extinguisher"}'
```

`tag_object` captures the image and its geometry *before* model inference, so
later robot motion does not change the observation's frame. If detection or
valid depth is insufficient it **errors** — change your viewpoint and retry; do
not use `tag_location` as a substitute.

**Tag a place / return point** (stand in a safe, convenient spot in the room):

```bash
dimos agent-send "Use tag_location to mark the current spot as office. Do not move."
dimos mcp call tag_location --json-args '{"location_name":"office"}'
```

The difference:

```text
tag_object("fire extinguisher") → the object's point-cloud estimate
tag_location("office")          → the robot's current position (a return point)
```

A place tag is a **return waypoint**, not the room's centre, and is *not*
projected onto a wall or doorframe. Incorrectly placed tags are **not**
auto-fixed.

## 5. Save and stop cleanly

```bash
dimos shell
# Python:
app.PersistentGo2Map.save_map()
```

You cannot save an unaligned map with no accepted scan. Then stop normally:

```bash
dimos stop
```

## 6. Restore the map (every later run)

Use the **same paths**, **without** `create-new=true`. Here is a manual-capture
restore:

```bash
dimos run unitree-go2-agentic-persistent \
  --robot-ip "$ROBOT_IP" \
  --persistentgo2map.map-file=assets/scene_maps/sedan_office_persistent/map.pc2.lcm \
  --persistentgo2map.manual-capture=true \
  --spatialmemory.scene-map-dir=assets/scene_maps/sedan_office_persistent \
  --mcpclient.model=gpt-5.6-luna \
  --no-obstacle-avoidance
```

This loads the old map and accumulates your captured scans, but **navigation,
aligned sensor forwarding, and map updates stay blocked** until you approve
alignment. There is no automatic motion; low-level manual control is not
fully disabled.

### 6.1 Capture scans

Drive slowly through a distinctive, overlapping part of the saved map and turn
to see different walls and corners. In `dimos shell`:

```python
app.PersistentGo2Map.alignment_status()   # accumulated scans + point counts
```

### 6.2 Stop and finish capture

**Release the remote and confirm the robot is stopped**, then:

```python
app.PersistentGo2Map.finish_startup_capture()
```

This freezes the accumulated cloud and begins matching. Too few points errors
and keeps capture active so you can continue. The RPC publishes a zero
velocity but cannot override a held physical remote.

### 6.3 Inspect the candidate

Wait for `Alignment candidate ready` in the log, then:

```python
app.PersistentGo2Map.alignment_status()
```

In Rerun compare:

| Entity | Content |
|---|---|
| `world/alignment_scan` | this session's captured scans |
| `world/alignment_preview` | the saved map placed into those coordinates by the candidate |

Check that **walls, corners, floor, robot position, and heading** all agree.
A high fitness or a single matching surface is **not** proof of a correct
match — symmetric corridors can score high with the wrong heading.

### 6.4 Approve or reject

Correct:

```python
app.PersistentGo2Map.confirm_alignment()
app.PersistentGo2Map.navigation_ready()   # should be True
```

Wrong:

```python
app.PersistentGo2Map.reject_alignment()
```

Rejecting resumes matching on the **same** captured cloud without moving the
robot. To capture a different sweep, stop and restart the restore flow. An
approved placement **cannot be undone online** — stop and restart. Alignment
approval is a human-only RPC; it is deliberately **not** exposed as an agent
skill.

After approval, new observations update the parts of the old map that were
seen; unseen parts are kept; coordinates remain the original world frame.

> Alignment confirmation is **only** an RPC — it is not an LLM tool. It does
> not add continuous global relocalization or remove odometry drift.

## 7. Then act

From another terminal, once aligned:

```bash
dimos agent-send "List the remembered locations without moving."
dimos agent-send "How many fire extinguisher tags are stored? List each ID and position."
dimos agent-send "Navigate to the fire extinguisher."
```

Check the path is clear and alignment is stable in Rerun **before** requesting
physical navigation.

## 8. Common problems

| Symptom | Check / fix |
|---|---|
| `create-new` says the map exists | Use the restore command; do not delete the existing map |
| `No module named persistentgo2map` | Shell uses `app.PersistentGo2Map`; CLI uses `--persistentgo2map.*` |
| Navigation rejected | Check `alignment_status()`; approve alignment before navigating |
| New tool missing | Restart the stack and check `dimos mcp list-tools` |
| Agent only returns "Message sent" | Use `dimos agentspy` to see the real answer |
| Object gets no depth | Check camera_info, TF, lidar/time alignment, object visibility; change the viewpoint — do not fake coordinates |
| Alignment keeps failing | Capture in a distinctive area; **do not** lower the threshold to force a placement |
| VLM HTTP 400 | Read the full server error; check the model name, permissions, and request params |
| Port 5555 / 9990 busy | `lsof -nP -iTCP:9990 -sTCP:LISTEN`; `dimos stop` only if it is a stale DimOS |

## 9. Command cheat-sheet

| Action | Command |
|---|---|
| Run status | `dimos status` |
| MCP status | `dimos mcp status` |
| MCP tools | `dimos mcp list-tools` |
| Module → skills | `dimos mcp modules` |
| Live agent messages | `dimos agentspy` |
| Recent log | `dimos log -n 100` |
| Follow log | `dimos log -f` |
| Chat | `dimos agent-send "..."` |
| Python console | `dimos shell` |
| All tags | `dimos mcp call query_memory_tags` |
| Cancel navigation | `dimos mcp call stop_navigation` |
| Save map | `app.PersistentGo2Map.save_map()` (in shell) |
| Stop cleanly | `dimos stop` |

## 10. Limits

- Matching with the Go2's **built-in** LiDAR is experimental; the preset was
  measured on a MID360.
- There is no continuous global relocalization or odometry-drift correction by
  default; PGO loop closure is opt-in (see
  [PGO loop closure](persistent-maps.md#pgo-loop-closure-optional)).
- The 3D map and semantic tags must share one world coordinate system.
- Old, wrong tags are not auto-repaired; an object position is a
  sensor-supported estimate.
- The persistent navigation gate is **not** a global hardware motion lock —
  direct low-level control can still move the robot.

See also [relocalization](../capabilities/navigation/relocalization.md),
[CLI usage](../usage/cli.md), [configuration](../usage/configuration.md), and
[the agent system](../capabilities/agents/index.md).
