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

import io
import json
import wave

import pytest
from unitree_webrtc_connect.constants import AUDIO_API, RTC_TOPIC

from dimos.robot.unitree.go2.reply_speaker import Go2ReplySpeaker


@pytest.fixture
def speaker(mocker):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\0\0")
    client = mocker.patch("dimos.robot.unitree.go2.reply_speaker.OpenAI")
    client.return_value.__enter__.return_value.audio.speech.create.return_value.content = (
        stream.getvalue()
    )
    filename = []

    def publish(topic, request):
        response = {"data": {"header": {"status": {"code": 0}}}}
        if request["api_id"] == AUDIO_API["UPLOAD_AUDIO_FILE"]:
            filename[:] = [json.loads(request["parameter"])["file_name"]]
        if request["api_id"] == AUDIO_API["GET_AUDIO_LIST"]:
            response["data"]["data"] = json.dumps(
                {
                    "audio_list": [{"CUSTOM_NAME": filename[0], "UNIQUE_ID": "uploaded-reply"}],
                }
            )
        return response

    boundary = mocker.Mock(side_effect=publish)
    return Go2ReplySpeaker(boundary, "test-only", "http://test/v1"), boundary, client


def test_speaker_is_opt_in_and_uses_robot_max_volume_and_uploaded_id(speaker):
    device, publish, client = speaker
    assert device.speak("Hello", 0) == "Reply speech cancelled."
    client.assert_not_called()
    generation = device.configure(True)["generation"]
    assert device.speak("Hello", generation) == "Reply played on Go2."
    calls = [
        (call.args[0], call.args[1]["api_id"], json.loads(call.args[1]["parameter"]))
        for call in publish.call_args_list
    ]
    assert calls[0] == (RTC_TOPIC["VUI"], 1003, {"volume": 10})
    assert (
        RTC_TOPIC["AUDIO_HUB_REQ"],
        AUDIO_API["SELECT_START_PLAY"],
        {"unique_id": "uploaded-reply"},
    ) in calls
    assert calls[-1] == (
        RTC_TOPIC["AUDIO_HUB_REQ"],
        AUDIO_API["SELECT_DELETE"],
        {"unique_id": "uploaded-reply"},
    )


def test_disable_during_tts_prevents_upload_and_playback(speaker):
    device, publish, client = speaker
    generation = device.configure(True)["generation"]
    create = client.return_value.__enter__.return_value.audio.speech.create
    response = create.return_value

    def disable(**kwargs):
        device.configure(False)
        return response

    create.side_effect = disable
    assert device.speak("Hello", generation) == "Reply speech cancelled."
    assert [call.args[1]["api_id"] for call in publish.call_args_list] == [1003, AUDIO_API["PAUSE"]]
    assert device.status()["enabled"] is False


def test_tts_and_playback_are_marked_busy_for_microphone_echo_suppression(speaker):
    device, _, client = speaker
    generation = device.configure(True)["generation"]
    create = client.return_value.__enter__.return_value.audio.speech.create
    response = create.return_value

    def generate(**kwargs):
        assert device.is_speaking
        return response

    create.side_effect = generate
    device.speak("Puppy", generation)
    assert not device.is_speaking


def test_rejected_volume_is_not_reported_as_enabled(speaker):
    device, publish, _ = speaker
    publish.side_effect = None
    publish.return_value = {"data": {"header": {"status": {"code": 7}}}}
    with pytest.raises(RuntimeError, match="status 7"):
        device.configure(True)
    assert device.status()["enabled"] is False


def test_missing_uploaded_audio_id_never_falls_back_to_filename(speaker, mocker):
    device, publish, _ = speaker
    generation = device.configure(True)["generation"]
    original = publish.side_effect

    def missing(topic, request):
        response = original(topic, request)
        if request["api_id"] == AUDIO_API["GET_AUDIO_LIST"]:
            response["data"]["data"] = '{"audio_list":[]}'
        return response

    publish.side_effect = missing
    with pytest.raises(RuntimeError, match="not playing"):
        device.speak("Hello", generation)
    assert AUDIO_API["SELECT_START_PLAY"] not in [
        call.args[1]["api_id"] for call in publish.call_args_list
    ]
