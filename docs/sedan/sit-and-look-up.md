# Sit to look up

The Go2's camera and lidar sit on **top of its body**. When the robot **sits**
(crouches on its haunches, or bends its hind legs), the top-mounted sensors
pitch further upward and can see higher and overhead objects. This is the same
one-tap action the Unitree Go2 app ships by default.

Sitting is worth it right before the robot needs to see **above eye level** —
for example:

- **Tagging a tall object** (a cabinet, a shelf, a doorframe) so the tag lands
  on the object instead of the floor below it.
- **Inspecting an elevated frontier** during exploration, so the camera and lidar
  see what is above or behind an obstacle.
- **A quick manual check** — "what's up there?" from chat or the web console.

> **Experimental.** The sit / rise behavior is unit-tested in simulation; the
> real crouch, balance, and recovery timing run on hardware. Supervise the
> robot and keep the area clear while it sits and rises.

## How it works

Sitting is a single Unitree sport command. The Go2 connection exposes it
(`sit`, `rise_sit`, `recovery_stand`) and a **`SitObserver`** skill wraps it
with the behavior a user actually wants: sit, hold long enough to capture the
upward view, then **rise and recover on its own** so the robot is walking-ready
again.

```text
sit → hold (capture the view) → rise_sit → recovery_stand → walk
```

- `sit` — crouch down. The top camera and lidar now pitch more upward.
- `rise_sit` — rise back to a standing posture.
- `sit_to_observe(seconds)` — sit, hold for `seconds`, then rise and run
  `recovery_stand` automatically. This is the "sit, look, stand" you call when
  you need the view for a few seconds.

`recovery_stand` is the Unitree recovery command that returns the robot to a
stance ready for the next motion command. It runs after rising so the robot is
walking-ready again.

## Three ways to use it

### 1. Automatic (the agent decides)

The agent can call `sit_to_observe` on its own when the scene needs a higher
view — for example, when it is about to tag a tall object or when an elevated
frontier is ahead. Because `SitObserver` is wired into the agentic blueprints,
the LLM sees it as a tool and chooses it from the scene. You can leave
`auto_sit_observe` off (the default) to let the agent decide per task, or turn
it on to make the robot sit proactively at certain points.

### 2. Manual from chat

Ask the running agent to sit — for example, *"sit down and look up at that
cabinet and tell me what it is."* The agent calls `sit_to_observe`, the robot
crouches, captures the view, and stands back up.

### 3. Manual from the web console

The web console has a **"Sit to look up"** button (Posture group). It calls the
same `sit_to_observe` skill and lets you set how long to stay seated. It is a
**human-only, supervised** action.

## Cute voice

The `ModeAnnouncer` announces sit with a playful line, reusing the robot's
voice (it never owns its own TTS), spoken **non-blocking**:

- **Sit** → *"Whoa — sitting up to look around, woof!"*
- **Rise** → *"All done — standing back up!"*

The lines are in the `cute_phrases.py` whitelist (keyed by `sit`,
`sit_to_observe`, `rise_sit`), so a new tag or a mode change still speaks its
own line.

## Configuration

`SitObserver` (config `SitObserverConfig`):

| Field | Default | Meaning |
|---|---|---|
| `sit_dwell_seconds` | `3.0` | How long to stay seated during `sit_to_observe` before rising. |
| `recovery_stand_after_sit` | `true` | Run `recovery_stand` after rising so the robot is walking-ready. |
| `auto_sit_observe` | `false` | Let a caller (e.g. exploration) sit the robot on its own; off = only explicit calls. |

## Under the hood

- **`UnitreeWebRTCConnection`** (`dimos/robot/unitree/connection.py`) exposes
  `sit()`, `rise_sit()`, `recovery_stand()` — the `Sit` (api 1009), `RiseSit`
  (1010), and `RecoveryStand` (1006) sport commands over `rt/api/sport/request`.
- **`SitObserver`** (`dimos/agents/skills/sit_observer.py`) is the skill
  container that the agent and the web console call.
- The `UnitreeSkillContainer` also exposes a generic `execute_sport_command`
  skill, so `execute_sport_command("Sit")` and `execute_sport_command("RiseSit")`
  work too; `SitObserver` is the ergonomic, auto-rising, cute-voice version.

## Safety

Sitting and rising are real motions. Supervise the robot, keep the area clear,
and let it finish rising and `recovery_stand` before it moves again. On
hardware, confirm the crouch and recovery timing feel right, then tune
`sit_dwell_seconds` and `recovery_stand_after_sit` to taste.
