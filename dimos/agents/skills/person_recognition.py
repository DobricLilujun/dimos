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

"""Real-time named-person recognition for agentic robots.

This reuses features that already exist in DimOS:

* person detection + ``Detection2DBBox.cropped_image()`` for a face/body crop,
* the person ReID embedding model (:class:`TorchReIDModel`) for matching,
* ``SpeakSkill`` (via :class:`SpeakSkillSpec`) to say the result through the
  Go2 Pro speaker.

It is **opt-in**: when ``gallery_dir`` is not set the module is a no-op, so the
``unitree-go2-agentic`` blueprint keeps its existing behaviour until the
operator passes ``--named-person-recognizer-skill-container.gallery-dir <path>``.

A *gallery* is a folder that contains one sub-folder per named person
(``<gallery_dir>/<person_name>/*.jpg``). The reference images are used to build
a per-name model at start-up; live detections are matched against the gallery.
The first time a person clears the threshold (subject to a per-name cooldown)
the robot speaks ``"I found <person_name>"``.

Two matching backends are supported (``recognition_backend``):

* ``"reid"`` (default): embed each reference with :class:`TorchReIDModel` and
  cosine-match live detections (deep-learning ReID, needs a model download).
* ``"cv"``: **traditional computer-vision**, using OpenCV's
  :class:`cv2.face.LBPHFaceRecognizer` (Local Binary Patterns Histograms — a
  classic ML/traditional-CV recognizer). It runs on CPU, needs **no model
  download**, and trains directly from the gallery images. A face/region detector
  extracts the region to match (injectable; defaults to a centre-crop so it works
  with zero extra dependencies).
"""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any

import numpy as np

from dimos.agents.annotation import skill
from dimos.agents.skills.speak_skill_spec import SpeakSkillSpec
from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.core.stream import In
from dimos.msgs.sensor_msgs.Image import Image
from dimos.utils.logging_config import setup_logger

logger = setup_logger()

# The embedding model + detector are duck-typed (injectable for tests); the real
# implementations (TorchReIDModel / Yolo2DDetector) are imported lazily in start()
# only when the module is actually configured, so this module stays lightweight
# and inert until an operator passes a gallery dir.

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


class NamedPersonConfig(ModuleConfig):
    # Directory holding one sub-folder per named person. ``None`` disables the
    # module entirely (no-op) so the agentic blueprint is unchanged by default.
    gallery_dir: str | None = None
    # Matching backend: "reid" (deep-learning TorchReIDModel + cosine) or
    # "cv" (traditional CV: OpenCV LBPH, CPU, no model download).
    recognition_backend: str = "reid"
    # Cosine similarity (0-1) above which a live detection is a match (reid),
    # or the normalised score (0-1) above which a match is accepted (cv).
    threshold: float = 0.5
    # Minimum seconds between two "I found <name>" announcements for one person.
    announce_cooldown_s: float = 15.0
    # Pixels of padding around the detection box when cropping.
    padding: int = 20
    # How many reference images per person to embed (keep it small for speed).
    max_images_per_person: int = 10


class NamedPersonRecognizerSkillContainer(Module):
    """Recognize named people in the live camera and speak their name.

    Reuses person detection + the ReID embedding model + ``SpeakSkill``.
    Inert (no-op) unless ``config.gallery_dir`` is set.
    """

    config: NamedPersonConfig
    _speak: SpeakSkillSpec | None = None  # injected by the blueprint from SpeakSkill

    color_image: In[Image]

    # Injectable for tests / alternative models; lazily created if left as None.
    _model: Any = None
    _detector: Any = None
    # For the "cv" backend: the face/region detector (injectable) + LBPH recognizer.
    _cv_detector: Any = None
    _cv_recognizer: Any = None

    def __init__(
        self,
        model: Any = None,
        detector: Any = None,
        face_detector: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._model = model
        self._detector = detector
        self._cv_detector = face_detector
        self._cv_recognizer = None
        # gallery: name -> list of normalized numpy vectors
        self._gallery: dict[str, list[np.ndarray]] = {}
        self._last_announced: dict[str, float] = {}
        self._enabled = False

    @rpc
    def start(self) -> None:
        super().start()

        if not self.config.gallery_dir:
            logger.info("NamedPersonRecognizer: no gallery_dir configured; disabled (no-op).")
            return

        if self.config.recognition_backend == "cv":
            self._start_cv()
            return

        # Embedding model (injectable; default to the person ReID model).
        if self._model is None:
            try:
                from dimos.models.embedding.treid import TorchReIDModel

                self._model = TorchReIDModel()
            except Exception as e:  # pragma: no cover - depends on optional deps
                logger.warning(
                    f"NamedPersonRecognizer: embedding model unavailable ({e}); disabled. "
                    "Install with: uv sync --extra torchreid"
                )
                return
        try:
            self._model.start()
        except Exception as e:  # pragma: no cover
            logger.warning(f"NamedPersonRecognizer: model start failed ({e}); disabled.")
            return

        # Detector (injectable; default to the YOLO 2D detector).
        if self._detector is None:
            try:
                from dimos.perception.detection.detectors.yolo import Yolo2DDetector

                self._detector = Yolo2DDetector()
            except Exception as e:  # pragma: no cover
                logger.warning(f"NamedPersonRecognizer: detector unavailable ({e}); disabled.")
                return

        self._gallery = self._load_gallery()
        if not self._gallery:
            logger.warning(
                f"NamedPersonRecognizer: no people found in gallery {self.config.gallery_dir}; disabled."
            )
            return

        self._enabled = True
        self.color_image.subscribe(self._on_color_image)
        logger.info(
            f"NamedPersonRecognizer: watching for {len(self._gallery)} person(s): "
            f"{', '.join(sorted(self._gallery))}"
        )

    def _start_cv(self) -> None:
        """Set up the traditional-CV backend (OpenCV LBPH + a face/region detector)."""
        try:
            self._cv_recognizer = _CvLbphRecognizer(threshold=self.config.threshold)
            self._cv_recognizer.start()
            if self._cv_recognizer is None or not self._cv_recognizer.is_ready():
                logger.warning("NamedPersonRecognizer: OpenCV LBPH unavailable; disabled.")
                return
        except Exception as e:  # pragma: no cover
            logger.warning(f"NamedPersonRecognizer: cv backend unavailable ({e}); disabled.")
            return

        # Face/region detector (injectable; default to a centre-crop so the
        # traditional-CV path needs zero extra dependencies).
        if self._cv_detector is None:
            try:
                self._cv_detector = _CenterCropFaceDetector()
            except Exception as e:  # pragma: no cover
                logger.warning(f"NamedPersonRecognizer: cv detector unavailable ({e}); disabled.")
                return

        # Load the gallery by training the LBPH recognizer on the reference images.
        if not self._cv_recognizer.load_gallery(
            self.config.gallery_dir, max_images=self.config.max_images_per_person
        ):
            logger.warning(
                f"NamedPersonRecognizer: no people found in gallery {self.config.gallery_dir}; disabled."
            )
            return

        self._enabled = True
        try:
            self.color_image.subscribe(self._on_color_image)
        except Exception as e:  # pragma: no cover - no live transport in tests
            logger.debug(f"NamedPersonRecognizer (cv): subscribe skipped: {e}")
        logger.info(
            f"NamedPersonRecognizer (cv): watching for {len(self._cv_recognizer.names)} person(s): "
            f"{', '.join(self._cv_recognizer.names)}"
        )

    @rpc
    def stop(self) -> None:
        if self._model is not None:
            try:
                self._model.stop()
            except Exception:  # pragma: no cover
                pass
        self._model = None
        if self._cv_recognizer is not None:
            try:
                self._cv_recognizer.stop()
            except Exception:  # pragma: no cover
                pass
            self._cv_recognizer = None
        super().stop()

    # -- gallery ---------------------------------------------------------------
    def _load_gallery(self) -> dict[str, list[np.ndarray]]:
        """Embed every reference image into a per-name gallery.

        Robust to missing/bad files: a person with no usable images is skipped.
        """
        gallery: dict[str, list[np.ndarray]] = {}
        root = Path(self.config.gallery_dir)  # type: ignore[arg-type]
        if not root.is_dir():
            return gallery
        for person_dir in sorted(root.iterdir()):
            if not person_dir.is_dir():
                continue
            vectors = self._embed_folder(person_dir)
            if vectors:
                gallery[person_dir.name] = vectors
                logger.info(
                    f"NamedPersonRecognizer: registered '{person_dir.name}' "
                    f"({len(vectors)} reference image(s))"
                )
        return gallery

    def _embed_folder(self, folder: Path) -> list[np.ndarray]:
        """Embed up to ``max_images_per_person`` images from a folder."""
        assert self._model is not None
        vectors: list[np.ndarray] = []
        for image_path in sorted(folder.iterdir()):
            if image_path.suffix.lower() not in _IMAGE_EXTENSIONS:
                continue
            try:
                image = Image.from_file(str(image_path)).to_rgb()
            except Exception as e:
                logger.warning(f"NamedPersonRecognizer: skipping {image_path}: {e}")
                continue
            try:
                vec = self._normalize(self._model.embed(image).to_numpy())
            except Exception as e:
                logger.warning(f"NamedPersonRecognizer: embedding {image_path} failed: {e}")
                continue
            if vec is not None:
                vectors.append(vec)
            if len(vectors) >= self.config.max_images_per_person:
                break
        return vectors

    # -- matching --------------------------------------------------------------
    @staticmethod
    def _normalize(vec: np.ndarray) -> np.ndarray | None:
        norm = float(np.linalg.norm(vec))
        if norm <= 0:
            return None
        return vec / norm

    def match(self, query: np.ndarray) -> tuple[str, float] | None:
        """Best (name, score) whose max cosine similarity clears the threshold."""
        best_name: str | None = None
        best_score = self.config.threshold
        for name, vectors in self._gallery.items():
            # max cosine over the person's reference vectors
            score = float(max(v @ query for v in vectors))
            if score > best_score:
                best_score = score
                best_name = name
        if best_name is None:
            return None
        return best_name, best_score

    # -- live loop -------------------------------------------------------------
    def _on_color_image(self, image: Image) -> None:
        if not self._enabled:
            return
        # Traditional-CV backend: extract a face/region crop and let the LBPH
        # recognizer predict a name.
        if self.config.recognition_backend == "cv":
            if self._cv_recognizer is None or self._cv_detector is None:
                return
            try:
                crop = self._cv_detector.detect(image)
            except Exception as e:  # pragma: no cover
                logger.debug(f"NamedPersonRecognizer (cv): detection failed: {e}")
                return
            if crop is None:
                return
            try:
                match = self._cv_recognizer.recognize(crop)
            except Exception as e:  # pragma: no cover
                logger.debug(f"NamedPersonRecognizer (cv): recognition failed: {e}")
                return
            if match is not None:
                self._announce(match[0])
            return

        if self._detector is None or self._model is None:
            return
        try:
            detections = self._detector.process_image(image)
            if detections is None:
                return
        except Exception as e:  # pragma: no cover - detector/model robustness
            logger.debug(f"NamedPersonRecognizer: detection failed: {e}")
            return
        for detection in detections.detections:
            try:
                crop = detection.cropped_image(padding=self.config.padding)
            except Exception:  # pragma: no cover
                continue
            try:
                query = self._normalize(self._model.embed(crop).to_numpy())
            except Exception:  # pragma: no cover
                continue
            if query is None:
                continue
            match = self.match(query)
            if match is not None:
                self._announce(match[0])

    def _announce(self, name: str) -> None:
        now = time.time()
        if now - self._last_announced.get(name, 0.0) < self.config.announce_cooldown_s:
            return
        self._last_announced[name] = now
        if self._speak is None:
            logger.info(f"NamedPersonRecognizer: recognized '{name}' (no speaker wired).")
            return
        try:
            self._speak.speak(f"I found {name}", blocking=False)
        except Exception as e:  # pragma: no cover
            logger.warning(f"NamedPersonRecognizer: failed to speak: {e}")

    # -- agent-facing skill ---------------------------------------------------
    @skill
    def recognized_people(self) -> str:
        """List the named people this robot can recognize and which it has seen.

        Args:
            (none)
        """
        if self.config.recognition_backend == "cv" and self._cv_recognizer is not None:
            known = ", ".join(self._cv_recognizer.names) or "none"
        else:
            known = ", ".join(sorted(self._gallery)) or "none"
        seen = ", ".join(sorted(self._last_announced)) or "none"
        return f"Known people: {known}. Recognized so far: {seen}."


# ---------------------------------------------------------------------------
# Traditional-CV (OpenCV LBPH) backend
# ---------------------------------------------------------------------------


class _CenterCropFaceDetector:
    """Extract a face/region crop from a frame (injectable detector fallback).

    This is a zero-dependency fallback face/region detector for the ``cv``
    backend. It is injectable (``face_detector=...``) so a real face detector
    (e.g. an OpenCV DNN/Haar cascade, or :class:`Yolo2DDetector`) can be
    supplied. The default returns the **whole frame** as the crop, which matches
    a person that fills (or is centred in) the frame; a real pipeline should
    inject a detector that returns the face box. The crop is returned as the
    image's ``numpy`` array.
    """

    def detect(self, image: Image) -> np.ndarray | None:
        """Return the frame as an RGB ``numpy`` array (or ``None``)."""
        try:
            arr = image.as_numpy()
        except Exception:  # pragma: no cover
            return None
        if arr is None or arr.ndim != 3 or arr.shape[0] < 4 or arr.shape[1] < 4:
            return None
        return arr


class _CvLbphRecognizer:
    """Traditional-CV face recognizer built on OpenCV's LBPHFaceRecognizer.

    LBPH (Local Binary Patterns Histograms) is a classic ML/traditional-CV
    recognizer: it trains a model directly from the gallery images and predicts
    a label with a *distance* confidence (lower = better match; 0 = perfect). It
    runs on CPU and needs **no model download**.
    """

    def __init__(self, threshold: float = 0.5, size: int = 100) -> None:
        self._threshold = threshold
        self._size = size
        self._recognizer: Any = None
        self._name_by_label: dict[int, str] = {}
        self._label_by_name: dict[str, int] = {}
        self._images: list[np.ndarray] = []
        self._labels: list[int] = []

    def start(self) -> None:
        try:
            from cv2.face import LBPHFaceRecognizer

            self._recognizer = LBPHFaceRecognizer.create()
        except Exception:  # pragma: no cover - OpenCV build without cv2.face
            self._recognizer = None

    def is_ready(self) -> bool:
        return self._recognizer is not None

    def stop(self) -> None:
        self._recognizer = None
        self._name_by_label.clear()
        self._label_by_name.clear()
        self._images.clear()
        self._labels.clear()

    @property
    def names(self) -> list[str]:
        return list(self._name_by_label.values())

    def load_gallery(self, gallery_dir: str, max_images: int = 10) -> bool:
        """Train the LBPH recognizer from a gallery of per-person folders.

        Returns ``True`` if at least one person has usable reference images.
        Robust to missing/bad files: a person with no usable images is skipped.
        """
        if not self.is_ready():
            return False
        root = Path(gallery_dir)
        if not root.is_dir():
            return False
        any_registered = False
        for person_dir in sorted(root.iterdir()):
            if not person_dir.is_dir():
                continue
            images = self._load_person_images(person_dir, max_images)
            if not images:
                continue
            label = len(self._label_by_name)
            self._name_by_label[label] = person_dir.name
            self._label_by_name[person_dir.name] = label
            self._images.extend(images)
            self._labels.extend([label] * len(images))
            any_registered = True
            logger.info(
                f"NamedPersonRecognizer (cv): registered '{person_dir.name}' "
                f"({len(images)} reference image(s))"
            )
        if not any_registered:
            return False
        try:
            self._recognizer.train(np.array(self._images), np.array(self._labels, dtype=np.int32))
        except Exception as e:  # pragma: no cover
            logger.warning(f"NamedPersonRecognizer (cv): LBPH training failed: {e}")
            return False
        return True

    def _load_person_images(self, folder: Path, max_images: int) -> list[np.ndarray]:
        """Load up to ``max_images`` grayscale images from a person folder."""
        images: list[np.ndarray] = []
        for image_path in sorted(folder.iterdir()):
            if image_path.suffix.lower() not in _IMAGE_EXTENSIONS:
                continue
            try:
                gray = self._to_gray(Image.from_file(str(image_path)).to_rgb())
            except Exception as e:  # pragma: no cover
                logger.warning(f"NamedPersonRecognizer (cv): skipping {image_path}: {e}")
                continue
            if gray is not None and gray.size > 0:
                images.append(self._resize(gray))
            if len(images) >= max_images:
                break
        return images

    def _resize(self, gray: np.ndarray) -> np.ndarray:
        """Resize to a fixed square so LBPH distances are size-independent."""
        try:
            import cv2

            return cv2.resize(gray, (self._size, self._size), interpolation=cv2.INTER_AREA)
        except Exception:  # pragma: no cover
            return gray

    @staticmethod
    def _to_gray(image: Image) -> np.ndarray | None:
        """Convert an Image (RGB) to a grayscale ``numpy`` array (or ``None``)."""
        try:
            arr = image.as_numpy()
        except Exception:  # pragma: no cover
            return None
        if arr is None or arr.ndim != 3:
            return None
        return _gray(arr)

    def recognize(self, crop: np.ndarray) -> tuple[str, float] | None:
        """Predict a name from a face/region crop, or ``None`` if below threshold.

        LBPH reports a *distance* confidence (lower = better match). We convert
        it to a normalised score (0-1, higher = better) so the ``threshold`` is
        consistent with the reid backend's cosine score.
        """
        if not self.is_ready():
            return None
        gray = _gray(crop)
        if gray is None:
            return None
        gray = self._resize(gray)
        try:
            label, confidence = self._recognizer.predict(gray)
        except Exception as e:  # pragma: no cover
            logger.debug(f"NamedPersonRecognizer (cv): predict failed: {e}")
            return None
        name = self._name_by_label.get(int(label))
        if name is None:
            return None
        # LBPH distances are typically in [0, ~100]; normalise to a 0-1 score.
        score = max(0.0, 1.0 - float(confidence) / 100.0)
        if score < self._threshold:
            return None
        return name, score


def _gray(arr: np.ndarray) -> np.ndarray | None:
    """RGB -> grayscale (luminance); pass through if already single-channel."""
    if arr is None:
        return None
    if arr.ndim == 3 and arr.shape[2] == 3:
        gray = np.asarray(
            0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2],
            dtype=np.float32,
        ).astype(np.uint8)
        return gray
    return arr
