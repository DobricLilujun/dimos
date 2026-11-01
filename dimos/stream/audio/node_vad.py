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

"""Voice-activity gate: forward only the frames that contain real speech.

A robot's own fan, motor whine, and HVAC hum are steady low-level signals that
the AGC happily boosts and that Whisper will try to transcribe as words. A VAD
sits between the mic and the normalizer/STT: it tracks a smoothed energy and
passes a frame through only while the energy is above the speech threshold,
collapsing everything else to silence. Speech bursts stay audible, fan noise is
dropped, and the STT sees shorter clips, so both latency and false
transcripts go down.
"""

from collections.abc import Callable

import numpy as np
from reactivex import Observable, create, disposable

from dimos.stream.audio.base import (
    AbstractAudioTransform,
    AudioEvent,
)
from dimos.stream.audio.volume import (
    calculate_rms_volume,
)
from dimos.utils.logging_config import setup_logger

logger = setup_logger()


class VoiceActivityGate(AbstractAudioTransform):
    """Pass audio through only while energy indicates speech activity.

    The gate tracks a smoothed energy estimate. A frame is considered "speech"
    when the energy is above ``speech_threshold``; it is considered "silence"
    after it drops below ``silence_threshold`` for ``silence_frames``
    consecutive frames, so brief gaps between words and syllables do not
    truncate a phrase. Non-speech frames are collapsed to near-silence so the
    STT downstream sees nothing to transcribe.
    """

    def __init__(
        self,
        speech_threshold: float = 0.06,
        silence_threshold: float = 0.025,
        silence_frames: int = 12,
        smoothing: float = 0.4,
        volume_func: Callable[[np.ndarray], float] = calculate_rms_volume,
    ) -> None:
        self.speech_threshold = speech_threshold
        self.silence_threshold = silence_threshold
        self.silence_frames = silence_frames
        self.smoothing = smoothing
        self.volume_func = volume_func

        self._energy = 0.0
        self._active = False
        self._silence_run = 0
        self.audio_observable = None

    def _gate_frame(self, audio_event: AudioEvent) -> AudioEvent:
        if audio_event.data.dtype != np.float32:
            audio_event = audio_event.to_float32()

        instantaneous = self.volume_func(audio_event.data)
        # Smooth the energy so a single spike or dip does not flip the gate.
        self._energy = (
            1 - self.smoothing
        ) * self._energy + self.smoothing * instantaneous

        if not self._active:
            if self._energy >= self.speech_threshold:
                self._active = True
                self._silence_run = 0
        else:
            if self._energy <= self.silence_threshold:
                self._silence_run += 1
                if self._silence_run >= self.silence_frames:
                    self._active = False
                    self._silence_run = 0
            else:
                self._silence_run = 0

        data = audio_event.data
        if not self._active:
            # Collapse non-speech to near-silence so STT has nothing to read.
            data = np.zeros_like(audio_event.data)
        return AudioEvent(
            data=data,
            sample_rate=audio_event.sample_rate,
            timestamp=audio_event.timestamp,
            channels=audio_event.channels,
        )

    def consume_audio(self, audio_observable: Observable) -> "VoiceActivityGate":  # type: ignore[type-arg]
        self.audio_observable = audio_observable  # type: ignore[assignment]
        return self

    def emit_audio(self) -> Observable:  # type: ignore[type-arg]
        if self.audio_observable is None:
            raise ValueError("No audio source provided. Call consume_audio() first.")

        def on_subscribe(observer, scheduler):
            logger.info(
                "Started voice-activity gate: speech=%s silence=%s frames=%d",
                self.speech_threshold,
                self.silence_threshold,
                self.silence_frames,
            )

            subscription = self.audio_observable.subscribe(
                on_next=lambda event: observer.on_next(self._gate_frame(event)),
                on_error=lambda error: observer.on_error(error),
                on_completed=lambda: observer.on_completed(),
            )

            def dispose() -> None:
                logger.info("Stopping voice-activity gate")
                subscription.dispose()

            return disposable.Disposable(dispose)

        return create(on_subscribe)
