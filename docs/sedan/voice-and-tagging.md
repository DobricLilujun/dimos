# Voice, VAD, and tag visualization

These features make the robot **talk in a playful puppy voice** as it changes
what it is doing, **listen faster and more reliably** by filtering out its own
fan noise, and **see and click its tagged objects** as glowing points in the web
console.

> **Experimental.** Full latency and noise testing is on real hardware. The
> behaviors below are unit-tested in simulation; supervise the robot and keep
> the area clear.

## Cute mode voice

When the agent enters or exits a *mode* — a skill that changes what the robot
is doing (explore, follow, navigate, tag …) — the robot speaks a short, playful
English one-liner. A **ModeAnnouncer** listens on the shared tool-stream and
speaks:

- **Enter** — the first time a mode's background stream goes active
  (e.g. `begin_demo_exploration` → *"Woof! Time to go explore — let's find all
  the new stuff!"*).
- **Exit** — when that stream reports itself stopped
  (e.g. `follow_person` → *"Follow's over — see you later!"*).

The lines are a small **whitelist** (`cute_phrases.py`) keyed by skill name, so
unknown tools stay silent. A mode is announced **once** per transition: repeated
progress frames for the same mode do not re-speak, and the exit line fires only
after the mode was actually entered.

A new tag speaks the same way: `tag_object` →
*"Woof! Tagged it — that's my territory now!"* The announcer reuses the robot's
voice (it never owns its own TTS) and speaks **non-blocking**, so a slow
synthesis never blocks the tool-stream thread.

| Mode | Entered | Exited |
|---|---|---|
| `begin_demo_exploration` / `begin_exploration` | "Woof! Time to go explore…" | "Exploring's done for now…" |
| `follow_person` | "I'm following you now — let's go go!" | "Follow's over — see you later!" |
| `navigate_with_text` | "On it! Heading there now." | — |
| `tag_object` | "Woof! Tagged it — that's my territory now!" | — |
| `stop_navigation` | — | "Okay, stopping now!" |

Configure `ModeAnnouncer`: `enabled` (default `true`) and `announce_unknown`
(`false` — unknown skills stay silent; set `true` for a neutral line on any
mode).

## Faster voice (fan noise)

The robot's own fan, motor whine, and HVAC hum are steady low-level signals
that the automatic-gain-control (AGC) happily boosts and that Whisper will try
to transcribe as words. Two layers fix this on the mic path:

```text
mic → VoiceActivityGate (drop fan/noise) → AGC normalizer (boost speech only) → Whisper
```

- **VoiceActivityGate** tracks a smoothed energy and passes a frame through only
  while the energy is above the speech threshold; brief gaps between words and
  syllables do not truncate a phrase. Non-speech frames collapse to
  near-silence, so Whisper sees shorter clips and fewer false transcripts.
- **AGC noise gate** — at or below the noise threshold the clip is treated as
  fan, not speech, so a low gain **floor** is applied instead of the AGC boost.
  A constant fan no longer pulls the AGC up and drowns out the next utterance.

The result is lower latency and fewer "word" hallucinations from the fan. The
reply side is **interruptible**: a fresh utterance can cut the robot off
mid-word with `Go2ReplySpeaker.interrupt()` (cancel the in-flight reply and
pause the audio hub), so the robot does not talk over the user. TTS and playback
are also flagged **busy** so the mic suppresses echo of the robot's own voice.

## Tag visualization (glowing points)

The web console renders every **tagged object** as a **glowing point**
(Resident-Evil style) over a 2D projection of world x/y:

- **Hover** shows a tooltip with **name / kind / position**.
- **Click** a point to **navigate near it** (reuses `navigate_near_memory_tag`).
- The same query results remain available as a table with a **Navigate nearby**
  button, so the glowing cloud and the list stay in sync.

The payload (`build_tag_viz_payload`) carries `name`, `kind`, `position`, and
`location_id` for each object tag; non-object tags (rooms, places, people) are
skipped so only taggable objects glow.

## Stricter auto-tagging

Object tagging dedup is stricter so the same object is not tagged repeatedly a
few metres or a few pixels away:

- **Exact name** within `tag_exact_name_distance_m` (1.0 m), or
- **Name similarity** ≥ `tag_dedup_name_similarity` (0.82) within the wider
  `tag_dedup_distance_m` cluster (1.6 m).

There is also a **ground-level preference**: a new ground-level tag near an
elevated same-named tag is a different instance and is kept; a new elevated tag
near a ground one is dropped as its duplicate, so the robot prefers tagging
things on the floor.

## Related

- [Web console](web-console.md) — the console and its control deck.
- [Demo exploration](demo-exploration.md) — the explore mode the voice announces.
- [Person following](person-following.md) — the follow mode.
- [Testing](testing.md) — how these are verified.
