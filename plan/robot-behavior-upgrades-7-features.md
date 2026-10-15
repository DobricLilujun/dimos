# PLAN: Robot behavior upgrades — exploration, voice, tagging, web viz (7 features)

## Context
Seven related requests to make the Go2 (SEDAN) stack more capable/interactive,
then document them on the live MkDocs site. Confirmed with the user:
- **All 7 in one plan.**
- **Test in simulation + basic unit tests**; the user will do real-hardware
  (mic/speaker/fan) testing themselves.
- **Rerun embed may change from cross-origin iframe to same-origin** (user OK).
- **"Mode" = agent-chat-driven skill transitions** (agent calls a tool like
  `begin_demo_exploration`/`follow_person`). Voice fires on **those skill
  transitions**, not a separate mode manager.
- **Voice language = English, playful/puppy tone.**

## Reuse (existing code to build on)
- `dimos/mapping/relocalization/go2/fusion_gate.py` — `FusionMotionGate`
  (stationary/rotation detection from odometry) → feature 1.
- `dimos/agents/skills/speak_skill.py` — `SpeakSkill.speak(text, blocking=False)`
  (OpenAI TTS via `SounddeviceAudioOutput`) → features 2, 5, 6.
- `dimos/agents/skills/speak_skill_spec.py` — `SpeakSkillSpec` (inject via `.remappings()`).
- `dimos/robot/unitree/go2/reply_speaker.py` — `Go2ReplySpeaker` (TTS over RTC
  audio hub, on-device) → features 2, 6 (hardware path).
- `dimos/agents/voice_input.py` — PTT → `whisper_pipeline` → `human_input`
  → feature 6.
- `dimos/stream/audio/` — `node_normalizer`, `node_microphone`, `pipeline.py`
  (whisper) → feature 6 (noise/latency).
- `dimos/navigation/experimental/frontier_exploration/demo_explorer.py` —
  `DemoExplorer.select_goal` / `_record_arrival` / `_navigate` /
  `begin_demo_exploration` → features 1, 3, 7.
- `dimos/navigation/experimental/patrolling/routers/frontier_patrol_router.py`
  — greedy scoring → feature 7.
- `dimos/perception/experimental/spatial_perception.py` —
  `_tag_object_location` (dedup = exact-name + ≤1.0 m) → feature 5.
- `dimos/agents/skills/navigation.py` — `tag_object`/`navigate_*`/`query_memory_tags` → feature 5.
- `dimos/agents/mcp/mcp_client.py` — `tool_stream` subscription (sees tool
  calls) → feature 2 (detect skill transitions).
- `dimos/web/console/frontend.py` (1376 lines) + `module.py` + `profiles.py`
  → features 4, 5.
- Docs: `docs/sedan/*.md`; build `mkdocs.github-pages.yml`; deploy
  `.github/workflows/pages.yml`.

---

## Feature 1 — Exploration: static/moving detection + recovery
**Where:** `demo_explorer.py` (recovery branch), reuse `fusion_gate.py`.
**What:** while navigating to a frontier goal, detect prolonged no-motion
(reuse `FusionMotionGate` logic or a stall timer). On stall, escalate:
rotate-in-place → back up a step → pick a nearby/new goal → (already partly
handled by `stall_timeout`). Never silently give up mid-run.
**Files:** `demo_explorer.py` (recovery in `_navigate`/loop), `fusion_gate.py`
(reuse thresholds), `DemoExplorerConfig` (new `stall_recovery` options).
**Tests:** unit — feed synthetic odometry stalls; assert recovery actions fire
(turn/back/new-goal) and exploration continues.

## Feature 2 — Cute English voice on skill/mode transitions
**Where:** new `dimos/agents/skills/mode_announcer.py` + `cute_phrases.py`;
reuse `SpeakSkill` / `Go2ReplySpeaker`.
**What:** a module that observes the agent's tool calls (via `tool_stream`,
like `McpClient` does) and speaks a short **playful English** line when a mode
skill is entered/exited (`begin_demo_exploration` → "On to exploring, let's
find new stuff!", `end_*`/`stop_*` → "All done, back to rest!",
`follow_person` → "I'm following you now!", turn/arrive → one-liners).
Phrase table keyed by skill name + enter/exit; de-dup repeated transitions.
**Files:** new `mode_announcer.py`, `cute_phrases.py`; wire into agentic
blueprint via `SpeakSkillSpec` remapping; `test_mode_announcer.py`.
**Tests:** unit — feed tool-stream notifications; assert the right phrase
spoken (mock speaker).

## Feature 3 — More effective exploration (never stop until fully explored)
**Where:** `demo_explorer.py`.
**What:** on stuck, generate random backward/nearby targets and keep moving
instead of exiting; only end when a **coverage/edge-traversal** check passes
(frontier clusters all visited or below a visited-coverage threshold, i.e.
"traverse the edges to confirm everything explored"). Keep existing
`failure_cooldown` but make low-gain completion stricter.
**Files:** `demo_explorer.py` (completion check + stuck recovery),
`ExploreOptions`/`DemoExplorerConfig` (new thresholds).
**Tests:** unit — synthetic costmap; assert it keeps moving through a stuck
episode and completes only when coverage passes.

## Feature 4 — Web Rerun: click-on-space → navigate
**Where:** `frontend.py`, `module.py`, rerun bridge; serve map **same-origin**.
**What:** change the Rerun embed from cross-origin iframe to a **same-origin
served map view** so a click can be read. On click, convert screen→world pose
and set a navigation goal (`NavigationInterfaceSpec.set_goal` or
`navigate_to`-equivalent). Keep the existing "open in new tab" fallback.
**Files:** `frontend.py` (embed + click handler → postMessage/HTTP),
`module.py` (new op to accept a clicked world pose and call `set_goal`),
`profiles.py` (enable for relevant profiles).
**Tests:** unit — pose-conversion helper + the set_goal call path (mock
navigation spec); manual sim check that a click starts a goal.
**Note:** cross-origin limitation is the main risk; same-origin is the fix the
user approved.

## Feature 5 — Auto-tagging upgrades
**Where:** `spatial_perception.py`, `navigation.py`, `frontend.py`, `speak_skill`.
- **(a) Visualization:** a toggle in the console that renders every tagged
  object in the (same-origin) Rerun/3D view as a **glowing point**
  (Resident-Evil-style); hovering shows a **tooltip** (name/kind/position);
  **clicking a point navigates** to it (reuse `navigate_near_memory_tag`).
- **(b) Ground emphasis + strict dedup:** prefer **ground-level** objects;
  tighten `_tag_object_location` dedup (current = exact-name + ≤1.0 m) to also
  match by **position cluster + name-similarity/bbox**, so the same object isn't
  tagged repeatedly.
- **(c) Cute "woof" on tag:** on a new tag, speak (feature 2 style) e.g.
  "Woof! Tagged the fire extinguisher — that's my territory now!".
**Files:** `spatial_perception.py` (dedup + ground filter),
`navigation.py`/new tag-viz op in `module.py`, `frontend.py` (glow + tooltip +
click), `cute_phrases.py` (tag phrase), `test_*.py`.
**Tests:** unit — dedup rejects a duplicate within cluster/similarity; tag-viz
payload includes name/kind/position; tag phrase fires.

## Feature 6 — Faster voice interaction (fan noise)
**Where:** `voice_input.py`, `dimos/stream/audio/` (normalizer, VAD),
`reply_speaker.py`, agent prompt.
**What:** cut latency and fight fan noise — AGC/noise gate + VAD on the mic
path, shorter STT buffering (partial/early-final), interruptible replies,
faster TTS; a command flow: "find the yellow bag on the ground" →
"Okay, I'll start" → navigate, with a follow-up ("which yellow bag?") when
multiple match.
**Files:** `voice_input.py` (shorter assembly/early-final),
`node_normalizer.py`/new VAD (noise gate/AGC), `reply_speaker.py`
(interruptible/shorter), `voice_input.py` tests.
**Tests:** unit — normalizer/VAD on a noisy synthetic clip; latency
expectations; "find X → speak + navigate" flow (mocked).
**Note:** full latency/noise testing is on real hardware (user).

## Feature 7 — Greedy exploration + stay in-scene
**Where:** `demo_explorer.py` `select_goal`, `frontier_patrol_router.py`,
`wavefront_frontier_goal_selector.py`.
**What:** prefer **short-distance greedy** targets (closest reachable frontier,
not long detours); place goals using the **2D vertical projection of the
point cloud / occupancy** so goals land in free space and the robot doesn't
aim outside the scene or circle. Reject goals outside the free-space boundary.
**Files:** `demo_explorer.py` (greedy `select_goal`, in-scene filter),
`frontier_patrol_router.py` (greedy scoring).
**Tests:** unit — assert selected goals are within free space and prefer
nearby frontiers; reject out-of-scene goals.

---

## Docs (all 7) → live site
- Update `docs/sedan/*.md` (exploration, web-console, logging/voice) with the
  new behaviors; add/extend a page for the new mode/voice + tagging-viz +
  rerun-click features.
- Rebuild `mkdocs.github-pages.yml`; push to `learning-branch`; verify the
  GitHub Pages deploy (`.github/workflows/pages.yml`) and that pages are live
  at `https://dobriclilujun.github.io/dimos`.
- Keep **DimOS Agents** branding; keep `sedan` code references.

## Verification
- **Sim:** `dimos --simulation run unitree-go2-agentic` — exercise explore
  (1/3/7), voice transitions (2), tag viz + dedup (5), rerun click (4).
- **Unit tests:** new `test_*.py` for each feature; run
  `.venv/bin/python -m pytest <files>` — all pass.
- **Lint:** `ruff` + `mypy` on changed files.
- **Deploy:** pages workflow green; live pages 200.
- **Hardware (user):** real mic/speaker/fan voice (2/6) — provided as
  sim-tested code the user will validate on the robot.
