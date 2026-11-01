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

"""A second, differently dressed person, made by recoloring the first one's texture.

The scene's person is photogrammetry: a dark navy top and beige trousers baked into
one texture atlas. Recoloring by colour class (instead of by UV region) keeps the
shading and leaves skin, hair and shoes alone, and means no new binary asset has to
be committed.
"""

import cv2
import numpy as np


def recolor_person_texture(png: bytes) -> bytes:
    """Turn the navy top white and the beige trousers dark blue; return PNG bytes."""
    image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Person texture is not a decodable image")

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hue = hsv[..., 0].astype(np.int32)
    saturation = hsv[..., 1].astype(np.int32)
    value = hsv[..., 2].astype(np.int32)

    navy = (hue >= 95) & (hue <= 135) & (saturation >= 40) & (value <= 140)
    # Skin is redder and more saturated than the trousers, so the hue and saturation
    # bounds keep faces and hands as they are.
    beige = (hue >= 12) & (hue <= 35) & (saturation >= 15) & (saturation <= 105) & (value >= 110)

    recolored = hsv.copy()
    # White: nearly no colour, bright, with a little of the original folds showing.
    recolored[navy, 1] = 12
    recolored[navy, 2] = np.clip(200 + value[navy] * 0.4, 0, 255)
    # Dark blue-grey trousers, keeping the original shading.
    recolored[beige, 0] = 108
    recolored[beige, 1] = 90
    recolored[beige, 2] = np.clip(value[beige] * 0.35, 0, 255)

    ok, encoded = cv2.imencode(".png", cv2.cvtColor(recolored, cv2.COLOR_HSV2BGR))
    if not ok:
        raise RuntimeError("Failed to encode the recolored person texture")
    return bytes(encoded.tobytes())
