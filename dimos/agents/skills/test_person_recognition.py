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

"""Fast unit tests for NamedPersonRecognizerSkillContainer.

These use injectable fake model / detector / speaker so they run without any
heavy dependencies (no torchreid/ultralytics). They exercise the core
behaviour: gallery matching, the announcement cooldown, the no-op default, and
the full detect -> embed -> match -> speak pipeline.
"""

import time

import numpy as np
import pytest

from dimos.agents.skills.person_recognition import NamedPersonRecognizerSkillContainer
from dimos.msgs.sensor_msgs.Image import Image


# -- fakes ---------------------------------------------------------------------
class FakeEmbedding:
    def __init__(self, vec) -> None:
        self._vec = np.asarray(vec, dtype=float)

    def to_numpy(self) -> np.ndarray:
        return self._vec


class FakeModel:
    """embed(image) -> FakeEmbedding, keyed by id(image) with a default fallback."""

    def __init__(self, default=None, mapping=None) -> None:
        self._default = np.asarray(default or [0.0, 0.0, 0.0], dtype=float)
        self._mapping: dict[int, np.ndarray] = mapping or {}

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def register(self, image, vec) -> None:
        self._mapping[id(image)] = np.asarray(vec, dtype=float)

    def embed(self, image) -> FakeEmbedding:
        return FakeEmbedding(self._mapping.get(id(image), self._default))


class FakeSpeaker:
    def __init__(self) -> None:
        self.said: list[str] = []

    def speak(self, text: str, blocking: bool = True) -> None:
        self.said.append(text)


class FakeDetection:
    def __init__(self, crop) -> None:
        self._crop = crop

    def cropped_image(self, padding: int = 0) -> object:
        return self._crop


class FakeDetections:
    def __init__(self, detections) -> None:
        self.detections = detections


class FakeDetector:
    def __init__(self, crop) -> None:
        self._crop = crop

    def process_image(self, image) -> FakeDetections:
        return FakeDetections([FakeDetection(self._crop)])


# -- cleanup: stop every module so its event-loop thread does not leak --------
_created: list[NamedPersonRecognizerSkillContainer] = []


@pytest.fixture(autouse=True)
def _stop_created_modules() -> None:
    yield
    for m in _created:
        try:
            m.stop()
        except Exception:
            pass
    _created.clear()


def make(**config) -> NamedPersonRecognizerSkillContainer:
    m = NamedPersonRecognizerSkillContainer(**config)
    _created.append(m)
    return m


# -- tests ---------------------------------------------------------------------
def test_noop_by_default() -> None:
    """With no gallery_dir the module is inert (no gallery, not enabled)."""
    m = make()
    assert m._enabled is False
    assert m._gallery == {}


def test_match_selects_best_person_above_threshold() -> None:
    m = make(threshold=0.5)
    m._gallery = {"alice": [np.array([1.0, 0.0, 0.0])], "bob": [np.array([0.0, 1.0, 0.0])]}
    # query closest to alice
    assert m.match(np.array([0.99, 0.01, 0.0])) == ("alice", pytest.approx(0.99))
    # query closest to bob
    assert m.match(np.array([0.0, 0.99, 0.0])) == ("bob", pytest.approx(0.99))


def test_match_below_threshold_returns_none() -> None:
    m = make(threshold=0.9)
    m._gallery = {"alice": [np.array([1.0, 0.0, 0.0])]}
    # orthogonal query -> cosine 0.0 < 0.9
    assert m.match(np.array([0.0, 1.0, 0.0])) is None


def test_announce_respects_cooldown() -> None:
    m = make(announce_cooldown_s=100.0)
    speaker = FakeSpeaker()
    m._speak = speaker
    m._announce("alice")  # first -> speaks (updates last_announced)
    m._announce("alice")  # second, within cooldown -> ignored
    assert speaker.said == ["I found alice"]
    # after the cooldown elapses it speaks again
    m._last_announced["alice"] = time.time() - 200.0
    m._announce("alice")
    assert speaker.said == ["I found alice", "I found alice"]


def test_no_speaker_does_not_raise() -> None:
    """When no speaker is wired, a recognition must not raise."""
    m = make(announce_cooldown_s=0.0)
    m._speak = None
    m._announce("alice")  # should just log, not raise
    assert m._last_announced["alice"] > 0


def test_full_pipeline_speaks_recognized_name() -> None:
    """detect -> crop -> embed -> match -> speak, end to end with fakes."""
    m = make(threshold=0.5, announce_cooldown_s=1.0)
    model = FakeModel()
    speaker = FakeSpeaker()
    m._model = model
    m._speak = speaker
    m._enabled = True
    m._gallery = {"alice": [np.array([1.0, 0.0, 0.0])]}

    crop = object()
    model.register(crop, [0.98, 0.02, 0.0])  # cosine ~0.98 with [1, 0, 0]
    m._detector = FakeDetector(crop)

    m._on_color_image(object())  # input image is irrelevant to the fakes
    assert speaker.said == ["I found alice"]


def test_pipeline_no_match_does_not_speak() -> None:
    m = make(threshold=0.5, announce_cooldown_s=0.1)
    m._gallery = {"alice": [np.array([1.0, 0.0, 0.0])]}
    m._enabled = True
    crop = object()
    model = FakeModel()
    model.register(crop, [0.0, 0.0, 1.0])  # orthogonal to alice -> no match
    m._model = model
    m._detector = FakeDetector(crop)
    m._speak = FakeSpeaker()

    m._on_color_image(object())
    assert m._speak.said == []


def test_load_gallery_uses_model(tmp_path, monkeypatch) -> None:
    """Reference images under <gallery_dir>/<person>/ are embedded into a gallery."""
    (tmp_path / "alice").mkdir()
    (tmp_path / "bob").mkdir()
    (tmp_path / "alice" / "a1.jpg").write_text("x")
    (tmp_path / "bob" / "b1.jpg").write_text("x")

    # Image.from_file returns a valid (fake) image regardless of path, so we need
    # no real image files; to_rgb() is a real Image method.
    def fake_from_file(path: str) -> Image:
        return Image.from_numpy(np.zeros((2, 2, 3), dtype=np.uint8))

    monkeypatch.setattr(Image, "from_file", staticmethod(fake_from_file))

    m = make(gallery_dir=str(tmp_path), threshold=0.5)
    m._model = FakeModel(default=[1.0, 0.0, 0.0])  # non-zero embeddings

    gallery = m._load_gallery()
    assert set(gallery) == {"alice", "bob"}
    assert all(len(v) >= 1 for v in gallery.values())
