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

from types import SimpleNamespace

import numpy as np
import pytest

from dimos.robot.unitree.go2 import puppy as puppy_module
from dimos.robot.unitree.go2.puppy import PuppyConversation, load_local_transcriber


@pytest.fixture
def puppy(mocker):
    mocker.patch.object(puppy_module.time, "monotonic", return_value=100.0)
    client = mocker.Mock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="Woof! That chair is saving a seat for me.")
            )
        ]
    )
    assistant = PuppyConversation(
        client,
        mocker.Mock(return_value="你好 Puppy"),
        mocker.Mock(return_value=b"test-jpeg"),
        mocker.Mock(return_value="played"),
        mocker.Mock(return_value=False),
        mocker.Mock(),
    )
    yield assistant
    assistant.stop()


def test_murmur_waits_ten_seconds_uses_camera_and_never_calls_tools(puppy):
    puppy._step(109.9)
    puppy.client.chat.completions.create.assert_not_called()
    puppy._step(110)
    request = puppy.client.chat.completions.create.call_args.kwargs
    assert request["model"] == "gpt-4o-mini"
    assert "tools" not in request
    assert "My name is puppy, built from sedan" in request["messages"][0]["content"]
    assert "English only" in request["messages"][0]["content"]
    assert request["messages"][-1]["content"][1]["image_url"]["url"] == (
        "data:image/jpeg;base64,dGVzdC1qcGVn"
    )
    puppy.speak.assert_called_once_with("Woof! That chair is saving a seat for me.")
    puppy._step(109)
    assert puppy.client.chat.completions.create.call_count == 1


def test_local_microphone_utterance_takes_priority_and_raw_audio_is_not_sent(puppy):
    voice = np.full(8000, 2000, dtype=np.int16)
    puppy.receive_pcm(voice)
    puppy.receive_pcm(np.zeros(11200, dtype=np.int16))
    puppy._step(101)
    audio = puppy.transcribe.call_args.args[0]
    assert audio.dtype == np.float32
    assert len(audio) == 19200
    messages = puppy.client.chat.completions.create.call_args.kwargs["messages"]
    assert messages[-1]["content"][0] == {"type": "text", "text": "你好 Puppy"}
    assert all(item["type"] != "input_audio" for item in messages[-1]["content"])
    assert puppy.emit.call_args_list[0].args[0]["source"] == "Go2 microphone"
    puppy.speak.assert_called_once()


def test_speaker_echo_and_tail_are_discarded(puppy, mocker):
    puppy.busy.return_value = True
    puppy.receive_pcm(np.full(8000, 2000, dtype=np.int16))
    puppy.busy.return_value = False
    clock = mocker.patch.object(puppy_module.time, "monotonic", return_value=100.5)
    puppy.receive_pcm(np.full(8000, 2000, dtype=np.int16))
    puppy.receive_pcm(np.zeros(11200, dtype=np.int16))
    assert puppy._utterances.empty()
    clock.return_value = 102
    puppy.receive_pcm(np.full(8000, 2000, dtype=np.int16))
    puppy.receive_pcm(np.zeros(11200, dtype=np.int16))
    assert puppy._utterances.qsize() == 1


def test_no_comment_while_person_is_speaking_and_no_backlog_while_busy(puppy):
    puppy.receive_pcm(np.full(8000, 2000, dtype=np.int16))
    puppy._step(110)
    puppy.client.chat.completions.create.assert_not_called()


def test_continuous_microphone_noise_cannot_starve_camera_comment(puppy):
    puppy.receive_pcm(np.full(8000, 2000, dtype=np.int16))
    puppy._step(110)
    puppy.client.chat.completions.create.assert_not_called()
    assert "microphone utterance" in puppy.status()["stage"]
    puppy._step(118)
    puppy.speak.assert_called_once()
    reply=[call.args[0] for call in puppy.emit.call_args_list if call.args[0]["role"] == "agent"]
    assert reply[0]["source"] == "Puppy · Murmur"


def test_empty_whisper_result_does_not_skip_due_camera_comment(puppy):
    puppy.transcribe.return_value=""
    puppy.receive_pcm(np.full(8000, 2000, dtype=np.int16))
    puppy.receive_pcm(np.zeros(11200, dtype=np.int16))
    puppy._step(110)
    puppy.speak.assert_called_once()
    assert "Waiting for next camera comment" == puppy.status()["stage"]
    puppy.busy.return_value = True
    puppy._step(150)
    puppy.client.chat.completions.create.assert_not_called()


def test_disable_during_model_response_prevents_speech(puppy):
    response = puppy.client.chat.completions.create.return_value

    def disable(**kwargs):
        puppy.stop()
        return response

    puppy.client.chat.completions.create.side_effect = disable
    puppy._step(110)
    puppy.speak.assert_not_called()
    puppy._step(120)
    assert puppy.client.chat.completions.create.call_count == 1


def test_missing_camera_and_audio_are_explicit_not_hallucinated(puppy):
    puppy.camera.return_value = None
    puppy._step(111)
    puppy.client.chat.completions.create.assert_not_called()
    assert any("no fresh camera" in call.args[0]["content"] for call in puppy.emit.call_args_list)
    assert any(
        "no Go2 audio frames" in call.args[0]["content"] for call in puppy.emit.call_args_list
    )


def test_local_whisper_uses_cpu_vad_and_filters_no_speech(mocker):
    model = mocker.patch("faster_whisper.WhisperModel")
    model.return_value.transcribe.return_value = (
        iter(
            [
                SimpleNamespace(text=" 你好 Puppy ", no_speech_prob=0.1),
                SimpleNamespace(text="hallucination", no_speech_prob=0.9),
            ]
        ),
        None,
    )
    transcribe = load_local_transcriber("base")
    assert transcribe(np.zeros(16000, dtype=np.float32)) == "你好 Puppy"
    model.assert_called_once_with("base", device="cpu", compute_type="int8")
    assert model.return_value.transcribe.call_args.kwargs == {"beam_size": 1, "vad_filter": True}


def test_stop_discards_queued_raw_audio(puppy):
    puppy._utterances.put_nowait(np.ones(16000, dtype=np.float32))
    puppy.stop()
    assert puppy._utterances.empty()


def test_murmur_can_be_paused_without_disabling_microphone_conversation(puppy):
    assert puppy.configure_murmur(False)["murmur"] is False
    puppy._step(111)
    puppy.client.chat.completions.create.assert_not_called()
    puppy.receive_pcm(np.full(8000, 2000, dtype=np.int16))
    puppy.receive_pcm(np.zeros(11200, dtype=np.int16))
    puppy._step(112)
    puppy.speak.assert_called_once()
    assert puppy.status()["last_reply_time"] is not None
    assert puppy.configure_murmur(True)["murmur"] is True


def test_turning_off_murmur_during_model_request_cancels_spontaneous_playback(puppy):
    response = puppy.client.chat.completions.create.return_value

    def disable(**kwargs):
        puppy.configure_murmur(False)
        return response

    puppy.client.chat.completions.create.side_effect = disable
    puppy._step(110)
    puppy.speak.assert_not_called()
