# Copyright 2025-2026 Dimensional Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Lightweight VLM caption provider for automatic scene annotation.

Talks to any OpenAI-compatible vision endpoint (e.g. vLLM).  Captions are
returned as plain text and can be stored alongside spatial-memory embeddings.
"""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np

from dimos.utils.logging_config import setup_logger

logger = setup_logger()

DEFAULT_VLM_PROMPT = (
    "Describe this scene in one concise sentence, identify the room or area type, "
    "and list up to 5 prominent objects with 2D bounding boxes. "
    "Reply as JSON: {\"caption\": \"...\", \"place\": \"...\", "
    "\"place_bbox\": [x1, y1, x2, y2], \"items\": "
    "[{\"name\": \"...\", \"bbox\": [x1, y1, x2, y2]}]}. "
    "Coordinates are integer pixel values in the original image. "
    "Use null for place if unknown; omit items if none are visible. "
    "Use place_bbox only for a visible localized area or entrance; use null for a room "
    "type inferred from the whole scene. Never invent a box for an unseen place. "
    "Output only JSON, no reasoning."
)


class VlmCaptionProvider:
    """Call a remote VLM to produce a caption/place/objects for an image."""

    def __init__(
        self,
        base_url: str,
        model: str,
        prompt: str = DEFAULT_VLM_PROMPT,
        max_tokens: int = 512,
        timeout: float = 30.0,
        api_key: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.prompt = prompt
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.api_key = api_key

    def caption(self, image: np.ndarray) -> str | None:
        """Return a caption string for ``image``, or ``None`` on failure.

        Args:
            image: BGR or RGB image as a numpy array.

        Returns:
            Caption text, or ``None`` if the VLM call failed.
        """
        import cv2

        success, encoded = cv2.imencode(".jpg", image)
        if not success:
            logger.error("VlmCaptionProvider: failed to encode image to JPEG")
            return None

        b64 = base64.b64encode(encoded.tobytes()).decode("utf-8")
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                        },
                        {"type": "text", "text": self.prompt},
                    ],
                }
            ],
            "max_tokens": self.max_tokens,
        }

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as e:
            logger.warning(f"VlmCaptionProvider: request failed: {e}")
            return None
        except json.JSONDecodeError as e:
            logger.warning(f"VlmCaptionProvider: invalid JSON response: {e}")
            return None

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            logger.warning(f"VlmCaptionProvider: unexpected response shape: {e}")
            return None

        if not content:
            return None

        caption = self._extract_caption(content)
        logger.info(f"VlmCaptionProvider: caption='{caption}'")
        return caption

    def place(self, image: np.ndarray) -> str | None:
        """Return the place/room name for ``image``, or ``None`` if unknown."""
        result = self._caption_and_place(image)
        return result.get("place") if result else None

    def caption_and_place(self, image: np.ndarray) -> tuple[str | None, str | None]:
        """Return ``(caption, place)`` for ``image``."""
        result = self.analyze(image)
        if result is None:
            return None, None
        return result.get("caption"), result.get("place")

    def analyze(self, image: np.ndarray) -> dict[str, Any] | None:
        """Return ``{caption, place, items}`` for ``image``.

        ``items`` is a list of ``{name, bbox}`` dictionaries when available.
        """
        raw = self.caption(image)
        if raw is None:
            return None
        parsed = self._parse_json_response(raw)
        if parsed is None:
            # Fallback: treat the whole output as a free-form caption with no place/items.
            return {"caption": raw, "place": None, "items": []}
        return parsed

    def _caption_and_place(self, image: np.ndarray) -> dict[str, Any] | None:
        """Call the VLM and parse the JSON response into ``{caption, place}``."""
        raw = self.caption(image)
        if raw is None:
            return None
        parsed = self._parse_json_response(raw)
        if parsed is None:
            # Fallback: treat the whole output as a free-form caption with no place.
            return {"caption": raw, "place": None}
        return parsed

    @staticmethod
    def _extract_caption(content: str) -> str:
        """Return the JSON-ish part of ``content``, stripping markdown fences."""
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            # Drop opening fence
            lines = lines[1:]
            # Drop closing fence if present
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        return text

    @staticmethod
    def _parse_json_response(content: str) -> dict[str, Any] | None:
        """Parse a JSON response with caption, place, and optional items."""
        text = VlmCaptionProvider._extract_caption(content)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"VlmCaptionProvider: failed to parse JSON: {text[:200]}")
            return None

        if not isinstance(data, dict):
            return None

        caption = data.get("caption")
        place = data.get("place")
        if place is not None and not isinstance(place, str):
            place = str(place)
        if place in ("null", "none", "None", ""):
            place = None

        if not isinstance(caption, str) or not caption:
            return None

        items = VlmCaptionProvider._parse_items(data.get("items"))
        place_boxes = VlmCaptionProvider._parse_items(
            [{"name": place or "place", "bbox": data.get("place_bbox")}]
        )

        return {
            "caption": caption.strip(),
            "place": place.strip() if place else None,
            "items": items,
            "place_bbox": place_boxes[0]["bbox"] if place_boxes else None,
        }

    @staticmethod
    def _parse_items(raw_items: Any) -> list[dict[str, Any]]:
        """Normalize VLM object list to ``[{name, bbox}]`` with integer bbox."""
        if raw_items is None:
            return []
        if not isinstance(raw_items, list):
            logger.warning(f"VlmCaptionProvider: unexpected items type: {type(raw_items)}")
            return []
        items = []
        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                continue
            bbox = entry.get("bbox")
            if isinstance(bbox, list) and len(bbox) == 4:
                try:
                    bbox = [int(float(v)) for v in bbox]
                except (ValueError, TypeError):
                    bbox = None
            else:
                bbox = None
            items.append({"name": name.strip().lower(), "bbox": bbox})
        return items

    def extract_place_name(self, caption: str) -> str | None:
        """Backward-compatible alias for clients that already have a caption string."""
        parsed = self._parse_json_response(caption)
        return parsed.get("place") if parsed else None
