#!/usr/bin/env python3
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

import numpy as np
import pytest

from dimos.stream.audio.base import AudioEvent
from dimos.stream.audio.node_normalizer import AudioNormalizer
from dimos.stream.audio.node_vad import VoiceActivityGate
from dimos.stream.audio.volume import calculate_rms_volume


def _frame(data: np.ndarray) -> AudioEvent:
    return AudioEvent(
        data=data.astype(np.float32),
        sample_rate=16000,
        timestamp=0.0,
        channels=1,
    )


def _fan(duration_s: float = 0.4, level: float = 0.01) -> np.ndarray:
    # A steady low-level fan hum.
    t = np.linspace(0.0, duration_s, int(duration_s * 16000), endpoint=False)
    return np.sin(2 * np.pi * 60 * t) * level


def _speech(duration_s: float = 0.4, level: float = 0.4) -> np.ndarray:
    # A louder burst that resembles a spoken word.
    t = np.linspace(0.0, duration_s, int(duration_s * 16000), endpoint=False)
    return np.sin(2 * np.pi * 200 * t) * level


def test_vad_gates_fan_to_silence():
    gate = VoiceActivityGate()
    # A long run of fan noise should be collapsed to (near) silence.
    for _ in range(50):
        out = gate._gate_frame(_frame(_fan()))
    assert calculate_rms_volume(out.data) < 1e-4


def test_vad_passes_speech_through():
    gate = VoiceActivityGate(silence_frames=12)
    # Speech above the threshold should activate the gate and pass through.
    out = None
    for _ in range(30):
        out = gate._gate_frame(_frame(_speech()))
    assert out is not None
    assert calculate_rms_volume(out.data) > 0.1  # speech is audible, not gated


def test_vad_ignores_brief_gap_between_words():
    gate = VoiceActivityGate(speech_threshold=0.06, silence_threshold=0.025, silence_frames=12)
    # Speech, a couple of quiet frames, then speech again: still "active".
    for _ in range(15):
        gate._gate_frame(_frame(_speech()))
    for _ in range(5):  # brief gap below the silence threshold
        gate._gate_frame(_frame(_fan(level=0.01)))
    out = gate._gate_frame(_frame(_speech()))
    assert calculate_rms_volume(out.data) > 0.1  # not truncated by the gap


def test_noise_gate_does_not_amplify_fan():
    # A fan-level clip should get the noise-floor gain, not the AGC boost.
    normalizer = AudioNormalizer(noise_gate_threshold=0.03, noise_gate_floor=0.02)
    out = normalizer._normalize_audio(_frame(_fan(level=0.01)))
    assert calculate_rms_volume(out.data) < 0.05  # not boosted up to ~1.0


def test_noise_gate_boosts_speech():
    normalizer = AudioNormalizer(
        adapt_speed=0.9, noise_gate_threshold=0.03, noise_gate_floor=0.02
    )
    out = normalizer._normalize_audio(_frame(_speech(level=0.4)))
    assert calculate_rms_volume(out.data) > 0.2  # speech is boosted


def test_vad_then_normalizer_reacts_to_speech_not_fan():
    # End-to-end: fan first (should not pull AGC up), then speech is boosted.
    gate = VoiceActivityGate(silence_frames=12)
    normalizer = AudioNormalizer(adapt_speed=0.5, noise_gate_threshold=0.03, noise_gate_floor=0.02)
    for _ in range(40):
        event = gate._gate_frame(_frame(_fan(level=0.01)))
        normalizer._normalize_audio(event)
    speech = normalizer._normalize_audio(
        gate._gate_frame(_frame(_speech(level=0.4)))
    )
    assert calculate_rms_volume(speech.data) > 0.2
