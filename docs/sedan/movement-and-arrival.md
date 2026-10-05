# Nearby & visual arrival, speech, and "Puppy"

This page covers the agent's **near-tag navigation**, optional **visual
arrival**, the **Go2 speaker** (TTS), and the console-only **"Puppy"** ambient
commentary + microphone conversation. All of these are built on
`PersistentGo2Planner`, `ReplySpeaker`, and `GO2Connection`.

> **These are not hardware stops.** Never treat a chat or button action as an
> emergency stop. Give motion commands in the web agent chat; the microphone
> conversation and ambient commentary **never** call motion tools.

## Nearby navigation

The console agent first queries, then calls the new
`navigate_near_memory_tag(location_id)`. The robot stops when its **horizontal
distance** to the chosen tag is within the configured threshold — it does **not**
require exact height or heading, and if already in range it does not start
moving.

| Control | Where | Range / default |
|---|---|---|
| **Nearby stop distance** (live) | chat-panel slider | 0.3–3.0 m, step 0.1 m, default 1 m |
| **Nearby stop distance** (saved) | Settings, or `--persistentgo2planner.nearby-arrival-distance` | sets the restart default |

- The live slider applies to the **next** nearby navigation; the current trip
  keeps the threshold it started with. After a PGO loop the goal is refreshed by
  the same tag ID and keeps the threshold.
- The live slider is **not** persisted across restarts; the saved value sets the
  next-start default.
- This does **not** bypass map alignment, avoidance, or the planner readiness
  check. "Started navigating" is **not** arrival.
- The original precise tools (`navigate_to_memory_tag`, `navigate_with_text`,
  …) remain available for exact navigation.

## Visual arrival (opt-in)

Default **Visual arrival: Off** — stop only on the distance threshold. Enabling
it (chat panel **Visual arrival**, after confirming the turn prompt) makes the
*next* nearby navigation two-stage:

1. Reach the distance threshold and stop path planning / forward motion, but
   **do not** announce arrival yet.
2. Compare the current camera image with the tag's reference image. If no
   match, turn in place in short pulses at **0.15 rad/s** (each pulse ≤ 0.5 s,
   then stop to match), searching up to **20 s**. No turning during image
   computation. On match, stop and announce arrival.

Matching uses **local OpenCV ORB features + RANSAC** geometric consistency — no
image is uploaded. The object reference is the crop inside the tag box; the
place reference is the image at tag time.

- It is a **conservative** similarity check, **not** object identity — it can
  fail on plain, textureless, repetitive, or very different lighting/viewpoint.
- It will **not** keep moving or walk around an object to match.
- On timeout, a missing/old reference, a stale camera (> 3 s), or a match-
  interface failure, it stops and reports the reason in the chat tool log — it
  does **not** announce arrival.
- Old tags without a reference can be re-tagged to add one.
- `Stop navigation` / keyboard stop cancels the search; turning off Visual
  arrival cancels a search in progress. A PGO loop pause interrupts the search;
  after map + tag sync it re-checks the distance and searches again, and a stale
  match does not resume motion.
- **Confirm the area around the robot is safe to turn in before enabling.**

## Go2 speaker (TTS)

- Default **Off**. Confirm max volume, microphone, and model privacy prompts
  before enabling.
- It replays the agent's **final replies** through the Go2's **own audio** (not
  the computer's speaker) at volume 10/10; clicking again closes and pauses.
- **Replay / simulation mode is explicitly rejected.**
- TTS uses Settings' VLM URL (auto-appends `/v1`), the `.env` OpenAI key, and
  `tts-1`; the service must support audio generation, or the error shows in the
  Backend console.
- It only speaks the **final replies enabled after turning on** — not the user's
  input, tool calls, or tool results. Reply length is capped at 4096 chars and
  audio at 90 s; over-limit reports an error rather than truncating. Turning off
  cancels queued and in-progress replies; uploaded temporary audio is cleaned up
  on normal completion.
- When the planner confirms arrival it plays **"Woof! We've arrived!"** (with the
  speaker enabled); cancel, failure, or a PGO pause do **not** announce arrival.

## "Puppy" ambient commentary & microphone conversation (console-only)

**Default off.** Enabling **Go2 speaker** plus the privacy prompts also enables
Puppy ambient commentary and Go2 microphone listening; turning the button off
stops both. The chat panel has a separate **Murmur: On/Off** toggle that only
pauses/resumes ambient commentary (it does not stop the enabled microphone
conversation or the reply broadcast). The status line shows the model, whether
the camera is fresh, playback busy, and the last error.

- If it shows **Old/non-Puppy stack**, stop the robot stack and restart the
  console before starting the stack — refreshing the page or enabling the
  speaker on an old stack does not start murmur.
- Puppy's name is **"My name is puppy, built from sedan."** Ambient commentary,
  voice replies, and the console agent's final replies are in **English**; local
  Whisper still recognizes Chinese and English.
- When idle and the camera is fresh, it produces one short, cute ambient
  comment roughly every 10 s. Model request and playback do not overlap; timing
  restarts after playback, so it is not strictly every 10 s.
- Ambient commentary and conversation default to `gpt-4o-mini`, reusing
  Settings' VLM URL and OpenAI key. Local recognition uses
  **`faster-whisper` `base`** (CPU/int8); the first enable may download the
  model — wait. Override with `--go2connection.puppy-model` and
  `--go2connection.puppy-whisper-model`.
- **Raw microphone audio is recognized locally in memory only** — it is not
  uploaded or recorded. The recognized text and the current camera image are
  sent to the configured model, and the reply text is sent to TTS. **Do not
  enable in private or sensitive settings.** There is no wake word; valid speech
  it hears may trigger a reply.
- **Half-duplex with echo protection:** while the robot speaks and for ~1 s
  after, it does not listen and cannot be interrupted. The mic original, Puppy
  replies, and errors show in the chat panel and are not re-broadcast.
- **Microphone conversation does not call motion tools** — give navigation
  commands in the web agent chat. Missing mic, model, or audio-interface errors
  are shown. **Offline tests do not prove the real-hardware speaker/mic are
  accepted; firmware support still requires a Go2 to verify.**

## Microphone noise reduction

The console enables lightweight local noise reduction by default
(Settings → Agent & vision → **Puppy microphone noise reduction**); a direct
start of the console blueprint also enables it by default, with
`--go2connection.puppy-noise-reduction=false` to turn it off. Non-console
configs are unchanged.

- Before listening, stay quiet ~1 s so the system learns the robot's steady
  background noise; the status line shows calibration progress and Noise
  reduction On/Off.
- It then suppresses low-frequency and background spectrum, then does volume
  banding and local Whisper VAD/recognition — no added model and no uploaded
  raw recording. Speech during calibration does not enter recognition.
- If someone speaks during calibration or the motor noise changes, turn the
  speaker off and on again to recalibrate in a quiet state. Low-SNR speech may
  be attenuated; this is not a guarantee of recognition and does not remove all
  collision sounds.
- It is still **half-duplex**: during broadcast and the post-broadcast echo
  protection it does not recognize human speech. Full-duplex would need
  synchronized playback reference audio for echo cancellation, an interruption
  mechanism, and recognition scheduling — it is not a simple removal of the
  current listen pause. This version does **not** enable full-duplex; its extra
  CPU/memory/model cost must be measured on the real run computer.

## Related

- [The web console](web-console.md)
- [Persistent maps & relocalization](persistent-maps.md)
- [Testing & verification](testing.md)
