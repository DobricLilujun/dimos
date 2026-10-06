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
| **Agent skills** | `move`, `speak`, `navigate`, … | + `navigate_to_memory_tag`, `navigate_near_memory_tag`, `tag_object`, `tag_location`, `query_*`, `navigate_with_text` |
| **Console / UI** | CLI (`dimos`), `dimos shell`, MCP server | `RobotConsoleModule` — web console (`:8090`), control deck + ChatGPT-style chat, SSE |
| **Speech** | `SpeakSkill` (TTS) | + Go2 speaker TTS, "Puppy" ambient commentary, microphone conversation |
| **CLI / blueprints** | `unitree-go2-agentic`, … | + `unitree-go2-agentic-persistent[-console]`, `unitree-go2-agentic-persistent-demo` |
| **Configuration** | `GlobalConfig` | + opt-in flags: `--gallery-dir`, `--scene-graph-server.port`, `--fusion-motion-gate.*`, … |

## What's new (and where to read it)

- [Quick start: the persistent workflow](sedan/quickstart.md) — create a map, restore it, align, query, navigate, and tag in a single session.
- [The web console](sedan/web-console.md) — `RobotConsoleModule`: a control deck and a ChatGPT-style chat, embedded or standalone.
- [Persistent maps & relocalization](sedan/persistent-maps.md) — save/restore maps, alignment, the fusion gate, and PGO loop closure.
- [Demo exploration](sedan/demo-exploration.md) — `DemoExplorer`: frontier + efficient (Dijkstra) strategies and gain-aware stopping.
- [Nearby & visual arrival, speech, and "Puppy"](sedan/movement-and-arrival.md) — navigate to a memory tag by proximity, visual arrival, and the Go2 speaker.
- [Person recognition](sedan/person-recognition.md) — recognize people from a gallery by ReID or LBPH.
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
