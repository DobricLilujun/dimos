# Perception: VLM captioning & object segmentation

Two new perception providers power **automatic scene annotation** and the
**object-tagging pipeline** (used by `tag_object` and the persistent map's VLM
tagging). Both are **pluggable** and fall back gracefully.

## VLM caption provider

`VlmCaptionProvider` calls any **OpenAI-compatible vision endpoint** (e.g. vLLM)
to produce a caption / place / objects for an image. Captions are returned as
plain text and can be stored alongside spatial-memory embeddings.

- The official OpenAI endpoint uses `max_completion_tokens`; other compatible
  endpoints retain `max_tokens`.
- HTTP failures log up to 4096 characters of the response body **after redacting
  the configured API key and image data** — so a failed call does not leak the
  key or the image into the log.
- Configured by `base_url`, `model`, the prompt, `max_tokens`, `timeout`, and an
  optional `api_key`.

### The default prompt

The default prompt asks the model to describe the scene, identify the room/area
type, and list up to 5 prominent objects with 2D bounding boxes, returning JSON:

```json
{
  "caption": "...",
  "place": "...",
  "place_bbox": [x1, y1, x2, y2],
  "items": [ { "name": "...", "bbox": [x1, y1, x2, y2] } ]
}
```

Rules the prompt enforces:

- Coordinates are **integer pixel values in the original image**.
- Use `null` for `place` if unknown; omit `items` if none are visible.
- Use `place_bbox` **only** for a visible localized area or entrance; use `null`
  for a room type inferred from the whole scene.
- **Never invent a box for an unseen place.** Output only JSON, no reasoning.

## Object segmentation

`ObjectSegmentationProvider` is the pluggable 2D segmentation step in the
object-tagging pipeline. It supports:

- **VLM bounding boxes** (baseline / fallback)
- **YOLOv8-seg instance segmentation** when `ultralytics` is installed

The caller projects the resulting **mask centroid into 3D** using camera
intrinsics + TF + lidar pointcloud.

### Implementations

| Provider | Behaviour |
|---|---|
| `VlmBboxSegmenter` | **Fallback**: treats the VLM bounding box itself as a rectangular mask |
| YOLO segmenter | Real instance segmentation; the mask is more selective than a box and reduces background inclusion |

`segment(image, item_name, bbox=None)` returns a binary `(H, W)` mask, or
`None` if segmentation failed.

## How this connects to object tagging

The pipeline in
[`tag_object` / VLM tagging](persistent-maps.md#tagging-object-vs-location) is:

```text
detection box
   └─ (optional) segmentation mask (YOLO > VLM box)
        └─ camera intrinsics + time-aligned TF
             └─ time-aligned point-cloud projection
                  └─ foreground surface
                       └─ world coordinates (saved as the tag)
```

Key behaviours:

- If there is **no valid point-cloud depth**, the automatic object tag is
  **skipped and logged** — it **never** falls back to the robot's position or a
  fixed distance.
- YOLO does not guarantee a valid instance mask for every object; a bounding box
  alone can include background, so the box segmenter is the conservative
  fallback.
- What is saved is a **surface estimate**, not a guaranteed geometric centre.
- Same-named objects whose estimates are within **1 m** are merged; more distant
  observations are kept separate.
- Camera calibration, camera TF, and lidar/image time alignment must be
  available **after** alignment approval.

## Configuration

In the console, **Settings → Agent & vision** sets the VLM API URL and VLM
model (used for both automatic tagging and the standalone console's manual
object tagging). On a direct start use
`--spatialmemory.vlm-model` and the auto-tagging flags:

```bash
--spatialmemory.vlm-enable-place-tagging=true \
--spatialmemory.vlm-enable-object-tagging=true \
--spatialmemory.vlm-distance-m=1.0 \
--spatialmemory.object-segmenter=yolo
```

- The model must support image input and return pixel coordinates.
- `vlm-distance-m=1.0` is a *travel-distance* trigger, not "call every second".

## Related

- [Persistent maps & relocalization](persistent-maps.md)
- [Person recognition](person-recognition.md)
