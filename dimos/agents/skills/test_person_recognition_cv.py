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

"""Tests for the traditional-CV (OpenCV LBPH) person-recognition backend.

These run on CPU with **no model download** (OpenCV's LBPHFaceRecognizer is
built-in), exercising the ``recognition_backend="cv"`` path.
"""

from __future__ import annotations

import threading

import numpy as np
from PIL import Image as PILImage, ImageDraw
import pytest

from dimos.agents.skills.person_recognition import (
    NamedPersonRecognizerSkillContainer,
    _CenterCropFaceDetector,
    _CvLbphRecognizer,
)
from dimos.msgs.sensor_msgs.Image import Image


# ---------------------------------------------------------------------------
# thread cleanup (cv tests start modules; stop them to avoid leaking threads)
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _stop_created_modules(monkeypatch):
    created: list = []
    orig = NamedPersonRecognizerSkillContainer.__init__

    def track(self, *args, **kwargs):
        created.append(self)
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(NamedPersonRecognizerSkillContainer, "__init__", track)
    yield
    for m in created:
        try:
            m.stop()
        except Exception:
            pass
    for _ in range(50):
        if not any(
            t.name and "event_loop" in t.name and t.is_alive() for t in threading.enumerate()
        ):
            break
        import time

        time.sleep(0.1)


def _lbph_available() -> bool:
    try:
        import cv2

        return hasattr(cv2, "face") and hasattr(cv2.face, "LBPHFaceRecognizer")
    except Exception:
        return False


# Skip the whole module if OpenCV's cv2.face (LBPH) is not available in this
# build. LBPH is a traditional-CV recognizer with no model download, but a
# minimal OpenCV build may omit the ``cv2.face`` sub-module.
pytestmark = pytest.mark.skipif(
    not _lbph_available(),
    reason="OpenCV cv2.face (LBPH) not available in this build",
)


class _FakeSpeaker:
    def __init__(self) -> None:
        self.said: list[str] = []

    def speak(self, text: str, blocking: bool = False) -> None:
        self.said.append(text)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _make_face(seed: int) -> np.ndarray:
    """Synthetic face-like image; LBPH can distinguish different seeds."""
    img = PILImage.fromarray(np.full((120, 120, 3), 30, np.uint8))
    draw = ImageDraw.Draw(img)
    rng = np.random.default_rng(seed)
    for _ in range(60):
        x, y = int(rng.integers(20, 100)), int(rng.integers(20, 100))
        c = int(rng.integers(120, 255))
        draw.ellipse([x - 5, y - 5, x + 5, y + 5], fill=(c, c, c))
    draw.rectangle([15, 15, 104, 104], outline=(255, 255, 255), width=4)
    return np.array(img)


def _make_gallery(path, alice_base: int = 0, bob_base: int = 200) -> None:
    path.mkdir(exist_ok=True)
    for person, base in [("alice", alice_base), ("bob", bob_base)]:
        d = path / person
        d.mkdir()
        for i in range(4):
            img = PILImage.fromarray(_make_face(base + i))
            img.save(d / f"{i}.jpg")


def _make_cv(gallery, speaker, **config) -> NamedPersonRecognizerSkillContainer:
    config.setdefault("recognition_backend", "cv")
    config.setdefault("threshold", 0.1)  # lenient: LBPH score is a normalised 0-1
    config.setdefault("announce_cooldown_s", 0.0)
    config.setdefault("max_images_per_person", 4)
    gallery_dir = None if gallery is None else str(gallery)
    m = NamedPersonRecognizerSkillContainer(gallery_dir=gallery_dir, **config)
    m._speak = speaker
    return m


def _cv_ready(m: NamedPersonRecognizerSkillContainer) -> bool:
    return m._cv_recognizer is not None and m._cv_recognizer.is_ready()


# ---------------------------------------------------------------------------
# inert by default
# ---------------------------------------------------------------------------
def test_cv_inert_by_default():
    m = _make_cv(None, _FakeSpeaker())
    m.start()
    assert m._enabled is False


# ---------------------------------------------------------------------------
# core: gallery + query + speak
# ---------------------------------------------------------------------------
def test_cv_backend_recognizes(tmp_path):
    g = tmp_path / "gallery"
    _make_gallery(g)
    sp = _FakeSpeaker()
    m = _make_cv(g, sp)
    m.start()
    assert m._enabled is True
    assert _cv_ready(m)
    assert sorted(m._cv_recognizer.names) == ["alice", "bob"]

    m._on_color_image(Image.from_numpy(_make_face(2)))  # alice's style
    assert sp.said == ["I found alice"]
    m._on_color_image(Image.from_numpy(_make_face(202)))  # bob's style
    assert sp.said == ["I found alice", "I found bob"]
    assert m.recognized_people() == "Known people: alice, bob. Recognized so far: alice, bob."


def test_cv_backend_no_match_no_speak(tmp_path):
    g = tmp_path / "gallery"
    _make_gallery(g, alice_base=0, bob_base=200)
    sp = _FakeSpeaker()
    m = _make_cv(g, sp, threshold=0.95)  # very strict: nothing clears it
    m.start()
    m._on_color_image(Image.from_numpy(_make_face(1)))
    assert sp.said == []


# ---------------------------------------------------------------------------
# unit: the LBPH recognizer + centre/whole-frame detector
# ---------------------------------------------------------------------------
def test_cv_lbph_recognizer(tmp_path):
    g = tmp_path / "gallery"
    _make_gallery(g)
    rec = _CvLbphRecognizer(threshold=0.1)
    rec.start()
    assert rec.is_ready()
    assert rec.load_gallery(str(g), max_images=4) is True
    assert sorted(rec.names) == ["alice", "bob"]
    match = rec.recognize(_make_face(2))
    assert match is not None and match[0] == "alice"


def test_center_crop_detector_returns_frame():
    arr = np.zeros((100, 100, 3), np.uint8)
    crop = _CenterCropFaceDetector().detect(Image.from_numpy(arr))
    assert crop is not None
    assert crop.shape == (100, 100, 3)


def test_cv_recognizer_no_match_returns_none(tmp_path):
    g = tmp_path / "gallery"
    _make_gallery(g, alice_base=0, bob_base=200)
    rec = _CvLbphRecognizer(threshold=0.99)  # impossible to clear
    rec.start()
    rec.load_gallery(str(g), max_images=4)
    assert rec.recognize(_make_face(1)) is None
