# Copyright 2026 Dimensional Inc.
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

import cv2
import numpy as np
import pytest

from dimos.perception.experimental.tag_view import match_tag_view


@pytest.fixture
def reference():
    rng = np.random.default_rng(7)
    image = np.full((400, 500, 3), 245, dtype=np.uint8)
    for x, y in rng.integers([20, 20], [480, 380], size=(160, 2)):
        color = tuple(int(value) for value in rng.integers(0, 210, size=3))
        cv2.circle(image, (int(x), int(y)), 5, color, -1)
    return image


def test_matching_same_view_accepts_geometrically_consistent_features(reference):
    transform = np.array([[0.95, 0.02, 12], [-0.01, 0.95, 14]], dtype=np.float32)
    current = cv2.warpAffine(reference, transform, (500, 400))
    result = match_tag_view(reference, current)
    assert result["matched"] is True
    assert result["inliers"] >= 12


def test_unrelated_camera_view_is_not_arrival(reference):
    different = np.random.default_rng(42).integers(0, 256, (400, 500, 3), dtype=np.uint8)
    assert match_tag_view(reference, different)["matched"] is False


def test_blank_tag_image_is_not_an_automatic_match(reference):
    result = match_tag_view(np.zeros_like(reference), reference)
    assert result == {"matched": False, "inliers": 0, "reason": "Insufficient visual texture"}
