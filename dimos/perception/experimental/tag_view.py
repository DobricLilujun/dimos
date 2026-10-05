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

from typing import Any

import cv2
import numpy as np


def match_tag_view(reference: np.ndarray, current: np.ndarray) -> dict[str, Any]:
    """Verify local feature correspondences, not merely semantic room similarity."""
    orb = cv2.ORB.create(nfeatures=1500)
    keys_a, descriptors_a = orb.detectAndCompute(reference, None)
    keys_b, descriptors_b = orb.detectAndCompute(current, None)
    if descriptors_a is None or descriptors_b is None:
        return {"matched": False, "inliers": 0, "reason": "Insufficient visual texture"}
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(descriptors_a, descriptors_b, k=2)
    good = [
        pair[0] for pair in pairs if len(pair) == 2 and pair[0].distance < 0.7 * pair[1].distance
    ]
    if len(good) < 12:
        return {"matched": False, "inliers": 0, "reason": "Too few corresponding features"}
    source = np.asarray([keys_a[match.queryIdx].pt for match in good], dtype=np.float32)
    target = np.asarray([keys_b[match.trainIdx].pt for match in good], dtype=np.float32)
    homography, mask = cv2.findHomography(source, target, cv2.RANSAC, 4.0)
    if homography is None or mask is None or not np.isfinite(homography).all():
        return {"matched": False, "inliers": 0, "reason": "No consistent image geometry"}
    accepted = mask.reshape(-1).astype(bool)
    inliers = int(accepted.sum())
    # Repeated texture confined to a tiny patch is not confirmation of the tag view.
    span = np.ptp(source[accepted], axis=0) if inliers else np.zeros(2)
    coverage = float(span[0] * span[1] / (reference.shape[0] * reference.shape[1]))
    matched = inliers >= 12 and inliers / len(good) >= 0.5 and coverage >= 0.08
    return {
        "matched": matched,
        "inliers": inliers,
        "reason": "Matching tag image" if matched else "Insufficient geometric agreement",
    }
