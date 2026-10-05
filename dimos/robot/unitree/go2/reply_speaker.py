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

import base64
from collections.abc import Callable
import hashlib
import io
import json
import threading
import time
from typing import Any
import uuid
import wave

from openai import OpenAI
from unitree_webrtc_connect.constants import AUDIO_API, RTC_TOPIC

from dimos.utils.logging_config import setup_logger

logger = setup_logger()


class Go2ReplySpeaker:
    """Opt-in audio-hub playback, independent of the existing host SpeakSkill."""

    def __init__(
        self,
        publish: Callable[[str, dict[str, Any]], Any],
        api_key: str | None,
        base_url: str | None = None,
    ) -> None:
        self._publish = publish
        self._api_key = api_key
        self._base_url = base_url
        self._lock = threading.RLock()
        self._disabled = threading.Event()
        self._disabled.set()
        self._generation = 0
        self._speaking = threading.Event()
        self._speech_lock = threading.Lock()

    @property
    def is_speaking(self) -> bool:
        return self._speaking.is_set()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"enabled": not self._disabled.is_set(), "generation": self._generation}

    def _request(self, topic: str, api_id: int, parameter: dict[str, Any]) -> dict[str, Any]:
        response = self._publish(topic, {"api_id": api_id, "parameter": json.dumps(parameter)})
        if not isinstance(response, dict):
            raise RuntimeError(f"Go2 audio API {api_id}: missing acknowledgement")
        data = response.get("data", {})
        code = (
            data.get("header", {}).get("status", {}).get("code") if isinstance(data, dict) else None
        )
        if code != 0:
            raise RuntimeError(f"Go2 audio API {api_id} failed (status {code})")
        return response

    def configure(self, enabled: bool) -> dict[str, Any]:
        # Set cancellation before waiting for an in-flight upload's lock.
        if not enabled:
            self._disabled.set()
        with self._lock:
            self._generation += 1
            if enabled:
                if not self._api_key:
                    raise RuntimeError("Go2 reply speech requires OPENAI_API_KEY in .env.")
                self._request(RTC_TOPIC["VUI"], 1003, {"volume": 10})
                self._disabled.clear()
            else:
                self._request(RTC_TOPIC["AUDIO_HUB_REQ"], AUDIO_API["PAUSE"], {})
            return self.status()

    def _active(self, generation: int) -> bool:
        return not self._disabled.is_set() and generation == self._generation

    def speak(self, text: str, generation: int) -> str:
        with self._speech_lock:
            self._speaking.set()
            try:
                return self._speak(text, generation)
            finally:
                self._speaking.clear()

    def _speak(self, text: str, generation: int) -> str:
        if not text.strip() or len(text) > 4096:
            raise ValueError("Reply speech requires 1-4096 text characters.")
        with self._lock:
            if not self._active(generation):
                return "Reply speech cancelled."
        with OpenAI(
            api_key=self._api_key, base_url=self._base_url, timeout=60, max_retries=0
        ) as client:
            audio = client.audio.speech.create(
                model="tts-1", voice="onyx", input=text, response_format="wav"
            ).content
        with wave.open(io.BytesIO(audio), "rb") as recording:
            samples = recording.readframes(recording.getnframes())
            duration = len(samples) / (
                recording.getsampwidth() * recording.getnchannels() * recording.getframerate()
            )
        if not 0 < duration <= 90:
            raise ValueError("Go2 reply audio must contain 1-90 seconds of audio.")
        filename = f"console_{uuid.uuid4().hex}"
        encoded = base64.b64encode(audio).decode("ascii")
        chunks = [encoded[i : i + 61440] for i in range(0, len(encoded), 61440)]
        file_md5 = hashlib.md5(audio).hexdigest()
        unique_id = None
        try:
            for index, chunk in enumerate(chunks, 1):
                with self._lock:
                    if not self._active(generation):
                        return "Reply speech cancelled."
                    self._request(
                        RTC_TOPIC["AUDIO_HUB_REQ"],
                        AUDIO_API["UPLOAD_AUDIO_FILE"],
                        {
                            "file_name": filename,
                            "file_type": "wav",
                            "file_size": len(audio),
                            "current_block_index": index,
                            "total_block_number": len(chunks),
                            "block_content": chunk,
                            "current_block_size": len(chunk),
                            "file_md5": file_md5,
                            "create_time": int(time.time() * 1000),
                        },
                    )
            with self._lock:
                if not self._active(generation):
                    return "Reply speech cancelled."
                response = self._request(
                    RTC_TOPIC["AUDIO_HUB_REQ"], AUDIO_API["GET_AUDIO_LIST"], {}
                )
                data = json.loads(response["data"]["data"])
                for item in data["audio_list"]:
                    if item.get("CUSTOM_NAME") == filename:
                        candidate = item.get("UNIQUE_ID")
                        if isinstance(candidate, str) and candidate:
                            unique_id = candidate
                        break
                if not isinstance(unique_id, str) or not unique_id:
                    raise RuntimeError(
                        "Go2 did not report an ID for the uploaded reply; not playing."
                    )
                self._request(
                    RTC_TOPIC["AUDIO_HUB_REQ"],
                    AUDIO_API["SET_PLAY_MODE"],
                    {"play_mode": "no_cycle"},
                )
                self._request(
                    RTC_TOPIC["AUDIO_HUB_REQ"],
                    AUDIO_API["SELECT_START_PLAY"],
                    {"unique_id": unique_id},
                )
            self._disabled.wait(timeout=duration)
            return "Reply speech cancelled." if self._disabled.is_set() else "Reply played on Go2."
        finally:
            if unique_id:
                with self._lock:
                    self._request(
                        RTC_TOPIC["AUDIO_HUB_REQ"],
                        AUDIO_API["SELECT_DELETE"],
                        {"unique_id": unique_id},
                    )
