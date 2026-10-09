# dimOS: what's new

This site documents the **newly-added dimOS features** and the tutorials that go
with them. It also keeps one comparison table that contrasts the original
(upstream) dimOS with these additions, so you can see exactly what is new.

`dimOS` is the agentic operating system for generalist robotics: Python-first,
agent-native, and hardware-agnostic. The original (upstream) dimOS already gave
you modules, blueprints, skills, an LLM agent, an MCP server, object detection
and tracking, frontier navigation, and a CLI. This site focuses on the features
that were added on top of that base.

## Comparison: original dimOS vs. the new features

The table below is the single side-by-side comparison of the upstream dimOS and
the new dimOS features documented here.

| Area | Original dimOS (upstream) | New dimOS features |
|---|---|---|
| **Maps** | Ephemeral `Go2Map`, rebuilt every run, not persisted | `PersistentGo2Map` — save and restore a map across runs (`map.pc2.lcm` + ChromaDB) |
| **Alignment / relocalization** | Re-locates on restore; PGO loop closure (opt-in) | Human-approved alignment (`confirm_alignment`); `FusionMotionGate` pauses permanent-map growth while the robot is stationary |
| **Planner** | `Go2Planner` plans toward a goal | `PersistentGo2Planner` — gates navigation until alignment is approved; adds nearby / visual arrival |
| **Exploration** | `WavefrontFrontierExplorer` | `DemoExplorer` — frontier + efficient (Dijkstra) strategies, gain-aware stopping, bounded startup scan |
| **Perception** | Object detection / tracking; object memory | `VlmCaptionProvider` (OpenAI-compatible VLM), object segmentation, `SceneGraphServerModule` |
| **People** | — | `NamedPersonRecognizerSkillContainer` — ReID / LBPH face recognition |
| **Person following** | `follow_person` — visual servoing straight to `cmd_vel`, no obstacle avoidance | `PersonNavigationSkillContainer` — tag, walk to and follow a person picked out by what they wear, planned around obstacles, with a turn-on-the-spot search when they are lost |
| **Dynamic goals** | The planner takes one goal at a time; nothing retargets it | `GoalTracker` — re-sends the goal when a moving target has moved far enough, stopping a set distance short |
| **Simulation** | MuJoCo office with one person and a 3 m lidar | Opt-in second person (`--mujoco-second-person`) and a longer lidar (`--mujoco-lidar-max-range`) |
| **Agent skills** | `move`, `speak`, `navigate`, … | + `navigate_to_memory_tag`, `navigate_near_memory_tag`, `tag_object`, `tag_location`, `query_*`, `navigate_with_text`, `tag_person`, `navigate_to_person`, `follow_person_with_planner`, `stop_following_person`, `describe_visible_people` |
| **Console / UI** | CLI (`dimos`), `dimos shell`, MCP server | `RobotConsoleModule` — web console (`:8090`), control deck + ChatGPT-style chat, SSE; blueprint and connection (robot / replay / MuJoCo) selectors, a People group and a live person follow distance |
| **Speech** | `SpeakSkill` (TTS) | + Go2 speaker TTS, "Puppy" ambient commentary, microphone conversation |
| **CLI / blueprints** | `unitree-go2-agentic`, … | + `unitree-go2-agentic-persistent[-console]`, `unitree-go2-agentic-persistent-demo`, `demo-unitree-go2-dynamic-goal`, `demo-unitree-go2-agentic-person-following`, `unitree-go2-agentic-persistent-person-following` (and its `demo-` MuJoCo variant) |
| **Configuration** | `GlobalConfig` | + opt-in flags: `--gallery-dir`, `--scene-graph-server.port`, `--fusion-motion-gate.*`, … |

## What's new (and where to read it)

- [Quick start: the persistent workflow](sedan/quickstart.md) — create a map, restore it, align, query, navigate, and tag in a single session.
- [The web console](sedan/web-console.md) — `RobotConsoleModule`: a control deck and a ChatGPT-style chat, embedded or standalone; pick a blueprint and a real robot, replay or MuJoCo connection.
- [Persistent maps & relocalization](sedan/persistent-maps.md) — save/restore maps, alignment, the fusion gate, and PGO loop closure.
- [Demo exploration](sedan/demo-exploration.md) — `DemoExplorer`: frontier + efficient (Dijkstra) strategies and gain-aware stopping.
- [Nearby & visual arrival, speech, and "Puppy"](sedan/movement-and-arrival.md) — navigate to a memory tag by proximity, visual arrival, and the Go2 speaker.
- [Person recognition](sedan/person-recognition.md) — recognize people from a gallery by ReID or LBPH.
- [Person tagging, navigation & following](sedan/person-following.md) — tag, walk to and follow a person by what they wear, with goal tracking and a lost-person search, in two MuJoCo demos.
- [Scene graph server](sedan/scene-graph.md) — expose a queryable scene graph over HTTP.
- [Perception: VLM captioning & object segmentation](sedan/perception-vlm.md) — an OpenAI-compatible VLM and object segmentation for tagging.
- [Testing & verification](sedan/testing.md) — what is verified offline and what requires real hardware.
- [CLI reference](sedan/cli.md) — the new blueprints and flags, and how to drive them.

## Safety

These features are **opt-in** and are layered on top of the existing Go2 stack;
the base `unitree-go2-agentic` blueprint is unchanged. Alignment confirmation is
a human-approved action, `--no-obstacle-avoidance` disables on-board obstacle
avoidance and adds none of its own, and the stop buttons and keyboard control
are **not** hardware emergency stops. See the [testing & verification](sedan/testing.md)
and the individual pages for the full safety notes.
