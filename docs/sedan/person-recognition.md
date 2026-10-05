# Person recognition

`NamedPersonRecognizerSkillContainer` gives the robot **real-time named-person
recognition** in the live camera. It reuses features that already exist in
DimOS — person detection, `Detection2DBBox.cropped_image()`, the ReID embedding
model (`TorchReIDModel`), and `SpeakSkill` (`SpeakSkillSpec`) — so the robot can
speak a person's name through the Go2 Pro speaker.

## Opt-in

The module is a **no-op** until an operator passes a gallery directory. So the
`unitree-go2-agentic` blueprint keeps its existing behaviour until:

```bash
--named-person-recognizer-skill-container.gallery-dir <path>
```

## The gallery

A *gallery* is a folder with one sub-folder per named person:

```text
<gallery_dir>/<person_name>/*.jpg
```

At start-up the reference images build a **per-name model**; live detections are
matched against it. The first time a person clears the threshold (subject to a
per-name cooldown) the robot speaks **`I found <person_name>`**.

## Two matching backends

Set with `recognition_backend`:

| Backend | How it works | Notes |
|---|---|---|
| `reid` (default) | Embed each reference with `TorchReIDModel` and **cosine**-match live detections | Deep-learning ReID; **needs a model download** |
| `cv` | Traditional computer vision: OpenCV `cv2.face.LBPHFaceRecognizer` (Local Binary Patterns Histograms) | Runs on CPU, **no model download**; trains directly from gallery images. A face/region detector extracts the region to match (injectable; defaults to a centre-crop so it works with zero extra dependencies) |

## Configuration

| Flag | Default | Meaning |
|---|---|---|
| `gallery_dir` | `None` (disabled) | Directory holding one sub-folder per person |
| `recognition_backend` | `reid` | `reid` or `cv` |
| `threshold` | `0.5` | Cosine similarity (reid) or normalised score (cv) above which a detection matches |
| `announce_cooldown_s` | `15.0` | Minimum seconds between two "I found `<name`" announcements for one person |
| `padding` | `20` | Pixels of padding around the detection box when cropping |
| `max_images_per_person` | `10` | How many reference images per person to embed (keep small for speed) |

## The skill

The module exposes one skill to the agent:

```bash
dimos mcp call recognized_people          # names currently recognized in view
```

`recognized_people` reports which named persons are currently recognized. The
announce path (the spoken "I found `<name`") is driven internally by
`_on_color_image` → `_match` → `_announce`, reusing `SpeakSkill`.

## Notes

- The detector and embedding model are **duck-typed and injectable** (so the
  module is testable without the real `TorchReIDModel` / `Yolo2DDetector`); the
  real implementations are imported lazily in `start()` **only** when the
  module is actually configured, so the module stays lightweight and inert until
  a gallery dir is passed.
- Supported image extensions: `.jpg .jpeg .png .bmp .webp`.

## Related

- [The web console](web-console.md)
- [Perception: VLM & segmentation](perception-vlm.md)
