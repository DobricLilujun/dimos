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

"""Pluggable 2D object segmentation for 3D scene annotation.

Supports:
- VLM bounding boxes (baseline/fallback)
- YOLOv8-seg instance segmentation when ``ultralytics`` is installed

The caller projects the resulting mask centroid into 3D using camera
intrinsics + TF + lidar pointcloud.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import numpy as np

from dimos.utils.logging_config import setup_logger

logger = setup_logger()


class ObjectSegmentationProvider(ABC):
    """Return a binary mask for an object of interest in an image."""

    @abstractmethod
    def segment(
        self,
        image: np.ndarray,
        item_name: str,
        bbox: list[int] | None = None,
    ) -> np.ndarray | None:
        """Return a binary mask (H, W) for ``item_name``.

        Args:
            image: BGR or RGB image as a numpy array.
            item_name: Semantic object name (e.g. "red chair").
            bbox: Optional VLM bounding box [x1, y1, x2, y2] to guide segmentation.

        Returns:
            Binary mask or ``None`` if segmentation failed.
        """
        ...


class VlmBboxSegmenter(ObjectSegmentationProvider):
    """Fallback segmenter that treats the VLM bbox itself as a rectangular mask."""

    def segment(
        self,
        image: np.ndarray,
        item_name: str,
        bbox: list[int] | None = None,
    ) -> np.ndarray | None:
        if bbox is None:
            return None
        height, width = image.shape[:2]
        x1, y1, x2, y2 = bbox
        x1, x2 = max(0, min(x1, width)), max(0, min(x2, width))
        y1, y2 = max(0, min(y1, height)), max(0, min(y2, height))
        if x2 <= x1 or y2 <= y1:
            return None
        mask = np.zeros((height, width), dtype=np.uint8)
        mask[y1:y2, x1:x2] = 1
        return mask


class YoloSegSegmenter(ObjectSegmentationProvider):
    """YOLOv8-seg based segmenter.

    Uses a lightweight off-the-shelf model (auto-downloaded by ultralytics) to
    produce instance masks. The best mask is chosen by:
    1. Class name match with ``item_name``
    2. IoU with the provided VLM bbox (if available)
    """

    _COCO_NAMES: set[str] = {
        "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
        "truck", "boat", "traffic light", "fire hydrant", "stop sign",
        "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep",
        "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
        "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard",
        "sports ball", "kite", "baseball bat", "baseball glove", "skateboard",
        "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork",
        "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
        "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
        "couch", "potted plant", "bed", "dining table", "toilet", "tv",
        "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave",
        "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
        "scissors", "teddy bear", "hair drier", "toothbrush",
    }

    def __init__(self, model: str = "yolov8n-seg.pt", conf: float = 0.25) -> None:
        from ultralytics import YOLO

        self._model = YOLO(model)
        self._conf = conf
        logger.info(f"Loaded YOLOv8-seg model: {model}")

    def segment(
        self,
        image: np.ndarray,
        item_name: str,
        bbox: list[int] | None = None,
    ) -> np.ndarray | None:
        try:
            results = self._model(image, conf=self._conf, verbose=False)
        except Exception as e:
            logger.warning(f"YOLOv8-seg inference failed: {e}")
            return None

        if not results or results[0].masks is None:
            return None

        result = results[0]
        masks = result.masks.data.cpu().numpy()  # (N, H, W)
        boxes = result.boxes.xyxy.cpu().numpy()  # (N, 4)
        classes = result.boxes.cls.cpu().numpy().astype(int)
        names = result.names

        if len(masks) == 0:
            return None

        # Normalize item name: take the last noun if multi-word.
        query_words = item_name.lower().split()
        target_names = set(query_words) & self._COCO_NAMES
        if not target_names:
            # No direct COCO match; try exact class name match from YOLO vocab.
            target_names = {item_name.lower()}

        scores: list[float] = []
        for i, cls_id in enumerate(classes):
            cls_name = names.get(cls_id, "").lower()
            mask = masks[i]
            score = 0.0

            # Semantic class match.
            if cls_name in target_names or any(w in cls_name for w in target_names):
                score += 2.0

            # Spatial overlap with VLM bbox.
            if bbox is not None:
                iou = self._mask_bbox_iou(mask, bbox)
                score += iou

            scores.append(score)

        if max(scores) <= 0:
            return None

        best_idx = int(np.argmax(scores))
        best_mask = (masks[best_idx] > 0.5).astype(np.uint8)
        logger.info(
            f"YOLOv8-seg selected '{item_name}' mask with score {scores[best_idx]:.2f}"
        )
        return best_mask

    @staticmethod
    def _mask_bbox_iou(mask: np.ndarray, bbox: list[int]) -> float:
        """Compute IoU between a binary mask and an axis-aligned bbox."""
        x1, y1, x2, y2 = bbox
        h, w = mask.shape
        x1, x2 = max(0, min(x1, w)), max(0, min(x2, w))
        y1, y2 = max(0, min(y1, h)), max(0, min(y2, h))
        if x2 <= x1 or y2 <= y1:
            return 0.0
        bbox_mask = np.zeros_like(mask)
        bbox_mask[y1:y2, x1:x2] = 1
        intersection = np.logical_and(mask, bbox_mask).sum()
        union = np.logical_or(mask, bbox_mask).sum()
        return float(intersection / union) if union > 0 else 0.0


def create_segmenter(prefer_torch: bool = True) -> ObjectSegmentationProvider:
    """Factory: use YOLOv8-seg if available, otherwise VLM-bbox fallback."""
    if prefer_torch:
        try:
            return YoloSegSegmenter()
        except Exception as e:
            logger.warning(f"Could not load YOLOv8-seg, falling back to VLM bbox: {e}")
    return VlmBboxSegmenter()


def mask_centroid(mask: np.ndarray) -> tuple[float, float]:
    """Return the centroid (cx, cy) of a binary mask."""
    ys, xs = np.where(mask > 0)
    if len(xs) == 0:
        raise ValueError("Empty mask")
    return float(xs.mean()), float(ys.mean())
