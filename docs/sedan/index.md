# SEDAN GROUP Additions

This section documents the **SEDAN GROUP additions** built on top of the
upstream [dimOS](../index.md) robotics stack. They turn the existing Go2
agentic robot into a **persistent, human-supervised, web-controllable**
system: it can remember a scene across sessions, align to a saved map,
explore a building on its own, recognize named people, and be driven from a
browser control deck with a ChatGPT-style chat.

> **Base commit.** Everything here is built on top of upstream commit
> `13e4a21fa` (*"plain go2 real time loop closure (#4331)"*). The home page
> [compares the original dimOS with these additions](../index.md); the pages in
> this section describe what the SEDAN GROUP **added** on top of it.

## What is dimOS (the base)?

dimOS is a Python-first, agent-native operating system for generalist
robotics. Its building blocks:

- **Modules** communicate through typed `In[T]` / `Out[T]` streams over
  LCM, ROS2, DDS, or shared memory.
- **Blueprints** compose modules into a runnable stack with `autoconnect()`.
- **Skills** (`@skill`) give an LLM agent the ability to act on hardware —
  `grab()`, `follow_object()`, `navigate_to_memory_tag()`, and so on.
- **McpServer / McpClient** expose those skills to an LLM over MCP.
- The **Go2 navigation stack** maps, plans, and drives a Unitree Go2
  quadruped, with PGO loop closure, costmapping, and frontier exploration.

The original (upstream) dimOS feature set is summarized in the [comparison
table on the home page](../index.md).

## What this section adds

| New feature | Module / blueprint | One line |
|---|---|---|
| **Persistent maps** | `PersistentGo2Map`, `PersistentGo2Planner` | Save a live map + semantic memory, restore it later, and *align to it with human approval* so the planner works in one stable world frame. |
| **Web console** | `RobotConsoleModule` | A browser control deck (port 8090) with buttons, a 3D/Rerun viewer, camera, and a ChatGPT-style chat that drives the agent. |
| **Demo exploration** | `DemoExplorer` | Frontier-based autonomous exploration with frontier *and* efficient (Dijkstra) strategies, gain-aware stopping, and bounded startup scanning. |
| **Nearby & visual arrival** | `PersistentGo2Planner` | Stop navigation when *near* a remembered tag (not at it), optionally confirm arrival with a local ORB image match. |
| **Person recognition** | `NamedPersonRecognizerSkillContainer` | Recognize named people in the live camera (embedding or OpenCV LBPH) and speak / report their name. |
| **Scene graph server** | `SceneGraphServerModule` | A queryable graph of objects, places, and their relations for the agent. |
| **Fusion gate** | `FusionMotionGate` | Auto-pause permanent-map growth while the robot is stationary (low-speed odometry), so maps don't thicken or drift at rest. |
| **PGO loop closure** | `PGOMap` | Optional pose-graph optimization that corrects the *current* run's map, tags, and pose together. |
| **Speech & "Puppy"** | `ReplySpeaker`, `GO2Connection` | Speak the agent's replies through the Go2's own audio; optional ambient commentary and half-duplex microphone conversation. |

All of these are **opt-in**: the original `unitree-go2-agentic` blueprint is
unchanged. You reach them through the new
`unitree-go2-agentic-persistent`, `unitree-go2-agentic-persistent-console`,
and `unitree-go2-agentic-persistent-demo` blueprints, or the standalone
`python -m dimos.web.console` console.

## How the pieces fit together

```
                         ┌───────────────────────────────┐
   Browser  ──:8090──▶   │      RobotConsoleModule        │
  (control deck          │  buttons · chat · viewer · 3D  │
   + ChatGPT chat)       └──────────────┬────────────────┘
                                        │ channel-name pub/sub
   ┌────────────────────────────────────┼─────────────────────────────┐
   │           PersistentGo2Map        │   PersistentGo2Planner        │
   │  save/restore/align + fusion gate │  refuse nav until aligned     │
   │  + PGO loop closure              │  nearby + visual arrival      │
   └───────────────┬───────────────────┴───────────────┬───────────────┘
                    │                                     │
        ┌──────────▼───────────┐              ┌─────────▼───────────┐
        │   SpatialMemory /    │              │   DemoExplorer      │
        │   scene graph / VLM  │              │  frontier explore   │
        └──────────────────────┘              └─────────────────────┘
```

1. **Create a map** (first run): drive slowly, the map + semantic memory save
   every 30 s and on stop.
2. **Restore + align** (later runs): load the map, capture scans, and a human
   confirms the alignment before any motion or tagging is allowed.
3. **Act**: query remembered tags, navigate (precisely or *nearby*), tag
   objects/places, explore the building — all from chat or buttons.
4. **Stay stable**: the fusion gate pauses map growth at rest; optional PGO
   corrects the current run's pose.

## The three entry points

| Command / blueprint | What it gives you |
|---|---|
| `dimos run unitree-go2-agentic-persistent` | The persistent stack, no web UI. You drive alignment, capture, and save from `dimos shell`. |
| `dimos run unitree-go2-agentic-persistent-console` | The same stack **plus** the embedded web control deck + chat (button-driven, with the original Rerun viewer preserved). |
| `python -m dimos.web.console` | The **standalone** console (port 8090). It manages its own robot stack as a subprocess; stop the existing stack first. |

> **Safety note.** Several of these flows disable on-board obstacle avoidance
> (`--no-obstacle-avoidance`) for manual capture and do not add their own
> avoidance. Operate slowly, keep the robot's footprint clear, and supervise
> it in person. These stop buttons are **not** a hardware emergency stop.

## Reading order

New to this? Start here:

1. [Overview](index.md) — you are here.
2. [Quick start: the persistent workflow](quickstart.md) — create, restore,
   align, query, navigate, and save.
3. [The web console](web-console.md) — the control deck that wraps the workflow.
4. The feature deep-dives, then [Testing & verification](testing.md) and
   [CLI reference](cli.md).

> **Experimental.** Matching with the Go2's *built-in* LiDAR is experimental —
> the alignment preset was measured on a MID360 sensor, not this one. A
> candidate that passes the algorithm's threshold is **not** proof of a
> correct match, which is why alignment always requires **human approval**.
