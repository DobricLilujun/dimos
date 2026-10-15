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

The Console's **Show tagged objects** toggle opens an interactive 3D object map.
The original Rerun view remains available, including its alignment preview.
Markers use stored world **x/y/z** over the point-cloud map, not the small,
normalized XY inventory chart:

- All stored object tags load without a manual memory query; updates are fetched
  while the view is visible. Rooms and places are not object markers.
- **Hover** shows the object's attributes, including name, description, stable
  ID, and XYZ position. Inspecting a marker does not require navigation readiness.
- **Click** follows the existing confirmed **Navigate nearby** operation using
  `navigate_near_memory_tag` and the stable tag ID, never a raw marker coordinate.
- The inventory query and table remain available. A missing/stale map or a
  WebGL failure is shown explicitly; it does not authorize robot movement.

The map renderer reuses the Cockpit Three.js scene. Its local asset is built
with `npm run build:console-map --prefix web/cockpit`; no external script CDN
or separate relay service is needed.

### Automatic-tagging speech

With **Go2 speaker** enabled, an actual successful off-to-on transition speaks
“Woof! Automatic tagging is on. Let's sniff out some treasures!” Status reads,
repeated enable requests, failed enablement, and paused tagging do not speak it.
Only successfully persisted **new objects** produce discovery announcements;
reobservations and failed writes do not. Short English puppy phrases rotate,
including “Woof! I tagged a … That's my territory now!”

Discovery speech is limited to one phrase per ten seconds and skipped while
other speech is queued. Skipped discoveries are not replayed later. Turning the
speaker off invalidates queued speech through the existing generation gate.
Tagging and map updates still work silently; playback uses the robot speaker,
not the host-computer `speak` tool. All new product text and speech remain English.

## Stricter auto-tagging

Automatic detection prioritizes portable objects on or near the floor, rather
than tagging every visible surface. Local point-cloud support is used where
available; the code does not assume world Z=0 is the floor. Important safety
objects such as wall-mounted fire extinguishers and first-aid kits are retained.
When local support is insufficient, height is unknown and the VLM's
ground-priority candidate is retained, not presented as geometrically verified.
Manual object tagging is not subject to this automatic preference.

Deduplication is atomic with object creation:

- Exact/normalized names match within `tag_exact_name_distance_m` (1.0 m XY).
- Similar names require similarity at least `tag_dedup_name_similarity` (0.82),
  within `tag_dedup_distance_m` (1.0 m), never a wider radius than exact matches.
- A separate 0.6 m height difference limit preserves vertically separated
  instances while tolerating modest depth noise. A floor/elevated reobservation
  no longer bypasses duplicate detection.
- Name normalization preserves non-ASCII text. Object tags do not merge into
  explicit room/place/person tags, and the nearest matching instance wins.
- Distinct detections in one frame cannot reuse the same instance ID.

These are geometric heuristics, not perfect physical-instance recognition.
Historical duplicate tags are not automatically deleted or renumbered; existing
IDs and saved navigation references remain valid.

## Related

- [Web console](web-console.md) — the console and its control deck.
- [Demo exploration](demo-exploration.md) — the explore mode the voice announces.
- [Person following](person-following.md) — the follow mode.
- [Testing](testing.md) — how these are verified.
