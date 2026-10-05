# CLI reference

This page lists the **new commands and flags** for the SEDAN GROUP additions.
Flags follow the usual dimOS form: `--<module>.<field>`, with field names
kebab-cased (e.g. `--persistentgo2map.map-file`). Every new feature is **opt-in**:
the original `unitree-go2-agentic` blueprint is unchanged.

## New blueprints

| Blueprint | Adds |
|---|---|
| `unitree-go2-agentic-persistent` | `PersistentGo2Map` + `PersistentGo2Planner` + fusion gate + VLM/segmentation + `ReplySpeaker`. No web UI. |
| `unitree-go2-agentic-persistent-console` | The persistent stack **plus** the embedded `RobotConsoleModule` (control deck + chat, original Rerun viewer preserved). |
| `unitree-go2-agentic-persistent-demo` | The persistent stack with `DemoExplorer` (frontier + efficient strategies) in place of the Wavefront explorer. |

Standalone console (independent of the robot stack; it manages its own stack):

```bash
python -m dimos.web.console            # browser → http://127.0.0.1:8090
python -m dimos.web.console --port 8092
```

Run a blueprint:

```bash
dimos run unitree-go2-agentic-persistent \
  --robot-ip "$ROBOT_IP" \
  --persistentgo2map.map-file=assets/scene_maps/sedan_office_persistent/map.pc2.lcm \
  --persistentgo2map.create-new=true \
  --spatialmemory.scene-map-dir=assets/scene_maps/sedan_office_persistent \
  --mcpclient.model=gpt-5.6-luna \
  --no-obstacle-avoidance
```

## Persistent map flags (`--persistentgo2map.*`)

| Flag | Default | Meaning |
|---|---|---|
| `map-file` | — | Path to the `map.pc2.lcm` map |
| `create-new` | `false` | First-run only; **refuses** to overwrite an existing map file |
| `manual-capture` | `false` | Manual alignment capture (mutually exclusive with `startup-rotation`) |
| `startup-rotation` | `false` | In-place startup rotation capture for a restored map |
| `rotation-speed` | `0.15` | Rotation speed (rad/s) |
| `rotation-duration` | `20.0` | Max rotation duration (s) |
| `sensor-timeout` | `1.0` | Stale-sensor timeout |
| `save-interval` | `30.0` | Autosave interval (s) |
| `voxel-size` | `0.05` | Voxel size for the scan union |
| `min-local-points` | `2000` | Minimum points before a scan may be accepted |
| `pgo-enabled` | `false` | Opt-in PGO loop closure (needs GTSAM) |
| `auto-pause-fusion` | `false` | Auto-pause permanent-map fusion on low-speed odometry (on by default in the console) |

### Fusion gate (`--persistentgo2map.*`, shared)

| Flag | Default | Meaning |
|---|---|---|
| `fusion-window` | `0.5` | Motion judgement window (s) |
| `fusion-stationary-duration` | `1.0` | Stationary dwell before pause (s) |
| `fusion-sensor-timeout` | `1.0` | Odometry timeout (must be ≥ the window) |
| `fusion-stop-speed` | `0.02` | Speed below which the robot is stationary (m/s) |
| `fusion-resume-speed` | `0.04` | Speed above which the robot resumes (m/s) |
| `fusion-stop-rotation-deg` | `2.0` | Rotation below which stationary (deg/s) |
| `fusion-resume-rotation-deg` | `3.0` | Rotation above which resume (deg/s) |

> The resume threshold must be higher than the stationary threshold to avoid
> oscillation. These are **not** calibrated on the current robot — tune to your
> stationary noise and minimum real speed.

## Persistent planner flags (`--persistentgo2planner.*`)

| Flag | Default | Range | Meaning |
|---|---|---|---|
| `navigation-speed-limit` | — | 0.10–0.55 | Saved default translation cap (m/s); the live chat slider is not persisted |
| `nearby-arrival-distance` | `1.0` | 0.3–3.0 | Distance threshold for `navigate_near_memory_tag` (m) |
| `visual-arrival-enabled` | `false` | — | Two-stage visual arrival (OpenCV ORB) for the next nearby navigation |

## Spatial memory & VLM (`--spatialmemory.*`)

| Flag | Default | Meaning |
|---|---|---|
| `scene-map-dir` | — | Scene directory (holds `map.pc2.lcm`, `chromadb_data/`, `vlm_tags_*.jsonl`) |
| `vlm-model` | — | Model for automatic tagging / captions |
| `vlm-enable-place-tagging` | `false` | Automatic room/place tagging |
| `vlm-enable-object-tagging` | `false` | Automatic object tagging |
| `vlm-distance-m` | `1.0` | Travel-distance trigger for tagging (not "every second") |
| `object-segmenter` | — | `yolo` for instance segmentation; otherwise the VLM box is the mask |

## Other opt-in features

| Flag | Default | Meaning |
|---|---|---|
| `--named-person-recognizer-skill-container.gallery-dir` | `None` | Enable named-person recognition with this gallery folder |
| `--named-person-recognizer-skill-container.recognition-backend` | `reid` | `reid` or `cv` (OpenCV LBPH, no model download) |
| `--named-person-recognizer-skill-container.threshold` | `0.5` | Match threshold |
| `--scene-graph-server.port` | `None` | Enable the scene-graph HTTP server on this port |
| `--go2connection.puppy-enabled` | `false` | Enable "Puppy" ambient commentary + microphone conversation |
| `--go2connection.puppy-model` | `gpt-4o-mini` | Puppy model |
| `--go2connection.puppy-whisper-model` | `base` | Local Whisper model (CPU) |
| `--go2connection.puppy-noise-reduction` | `false` | Puppy microphone noise reduction (on by default in the console) |
| `--go2connection.tts-url` | — | TTS endpoint for the Go2 speaker |
| `--no-obstacle-avoidance` | — | Disable on-board obstacle avoidance (manual capture; **no added avoidance**) |
| `--mcpclient.model` | — | The agent / chat model |

## MCP / agent commands

| Command | Purpose |
|---|---|
| `dimos mcp list-tools` | Full tool names + argument schemas (restart the stack after code changes) |
| `dimos mcp modules` | Module → skill mapping |
| `dimos mcp status` | MCP server status |
| `dimos mcp call query_memory_tags` | All remembered tags |
| `dimos mcp call query_memory_tags --json-args '{"query":"fire extinguisher"}'` | Substring filter |
| `dimos mcp call navigate_to_memory_tag --json-args '{"location_id":"loc_..."}'` | Precise navigation |
| `dimos mcp call stop_navigation` | Cancel navigation |
| `dimos agent-send "..."` | Send text to the agent |
| `dimos agentspy` | Watch agent messages live |
| `dimos shell` | Python console (`app.PersistentGo2Map.*`, `app.PersistentGo2Planner.*`) |

## Shell (Python)

```python
app.PersistentGo2Map.alignment_status()
app.PersistentGo2Map.confirm_alignment()
app.PersistentGo2Map.navigation_ready()
app.PersistentGo2Map.reject_alignment()
app.PersistentGo2Map.cancel_startup_rotation()
app.PersistentGo2Map.finish_startup_capture()
app.PersistentGo2Map.pause_fusion()
app.PersistentGo2Map.resume_fusion()
app.PersistentGo2Map.fusion_status()
app.PersistentGo2Map.save_map()

app.PersistentGo2Planner.cancel_goal()
app.PersistentGo2Planner.is_goal_reached()
```

## Related

- [Quick start: the persistent workflow](quickstart.md)
- [The web console](web-console.md)
- [Testing & verification](testing.md)
