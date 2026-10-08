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

from dimos.simulation.mujoco.person_texture import recolor_person_texture

# Colours as they appear in the person's texture, in BGR.
NAVY = (80, 40, 30)
BEIGE = (165, 200, 215)
SKIN = (130, 160, 220)
ORANGE_SHOE = (40, 90, 200)
GREY = (120, 120, 120)


def _encode(colors: list[tuple[int, int, int]]) -> bytes:
    image = np.zeros((4, 4 * len(colors), 3), np.uint8)
    for index, color in enumerate(colors):
        image[:, index * 4 : (index + 1) * 4] = color
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return bytes(encoded.tobytes())


def _hsv_of_blocks(png: bytes, count: int) -> list[tuple[int, int, int]]:
    image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    return [tuple(int(v) for v in hsv[0, index * 4 + 1]) for index in range(count)]


def _bgr_of_blocks(png: bytes, count: int) -> list[tuple[int, int, int]]:
    image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    return [tuple(int(v) for v in image[0, index * 4 + 1]) for index in range(count)]


def test_navy_becomes_white() -> None:
    (_, saturation, value) = _hsv_of_blocks(recolor_person_texture(_encode([NAVY])), 1)[0]

    assert saturation < 40
    assert value > 190


def test_beige_becomes_dark_blue() -> None:
    (hue, saturation, value) = _hsv_of_blocks(recolor_person_texture(_encode([BEIGE])), 1)[0]

    assert 100 <= hue <= 115
    assert saturation > 60
    assert value < 100


@pytest.mark.parametrize("color", [SKIN, ORANGE_SHOE, GREY])
def test_skin_shoes_and_neutral_colors_are_left_alone(color: tuple[int, int, int]) -> None:
    (result,) = _bgr_of_blocks(recolor_person_texture(_encode([color])), 1)

    assert max(abs(a - b) for a, b in zip(result, color, strict=True)) <= 3


def test_the_two_outfits_differ_clearly_in_brightness() -> None:
    top, trousers = _hsv_of_blocks(recolor_person_texture(_encode([NAVY, BEIGE])), 2)

    assert top[2] - trousers[2] > 100


def test_image_size_is_kept() -> None:
    png = _encode([NAVY, BEIGE, SKIN])

    result = cv2.imdecode(np.frombuffer(recolor_person_texture(png), np.uint8), cv2.IMREAD_COLOR)

    assert result.shape == (4, 12, 3)


def test_undecodable_bytes_are_rejected() -> None:
    with pytest.raises(ValueError, match="decodable"):
        recolor_person_texture(b"not a png")
