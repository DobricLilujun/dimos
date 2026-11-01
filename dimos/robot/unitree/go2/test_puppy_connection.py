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

import asyncio
from threading import Event, Thread

from av import AudioFrame
import numpy as np
import pytest

from dimos.robot.unitree.connection import UnitreeWebRTCConnection
from dimos.robot.unitree.go2 import connection as go2_module
from dimos.robot.unitree.go2.connection import GO2Connection


@pytest.fixture
def robot(mocker):
    connection = mocker.Mock(spec=UnitreeWebRTCConnection)
    connection.conn = mocker.Mock()
    connection.conn.audio.track_callbacks = []
    mocker.patch.object(go2_module, "make_connection", return_value=connection)
    module = GO2Connection(puppy_enabled=True)
    speaker = mocker.patch.object(module, "_robot_speaker").return_value
    speaker.configure.side_effect = lambda enabled: {"enabled": enabled, "generation": 2}
    model = mocker.patch.object(go2_module, "load_local_transcriber")
    puppy = mocker.patch.object(go2_module, "PuppyConversation")
    mocker.patch.object(go2_module, "OpenAI")
    yield module, connection, speaker, model, puppy
    module.stop()
    module.dispose()


def test_enabling_speaker_starts_local_asr_murmur_and_disabling_stops_microphone(robot, mocker):
    module, _, speaker, model, puppy = robot
    switch = mocker.patch.object(module, "_switch_puppy_audio")
    result = module.configure_reply_speaker(True)
    assert result == {"enabled": True, "generation": 2, "murmur": True, "microphone": True}
    model.assert_called_once_with("base")
    puppy.return_value.start.assert_called_once()
    assert puppy.call_args.kwargs["noise_reduction"] is False
    switch.assert_called_once_with(True)

    result = module.configure_reply_speaker(False)

    assert result["microphone"] is False
    assert switch.call_args.args == (False,)
    puppy.return_value.stop.assert_called_once()
    assert speaker.configure.call_args.args == (False,)
    assert module._puppy is None


def test_noise_reduction_setting_is_passed_without_changing_default_robot(robot, mocker):
    module, _, _, _, puppy = robot
    mocker.patch.object(module, "_switch_puppy_audio")
    mocker.patch.object(module.config, "puppy_noise_reduction", True)
    module.configure_reply_speaker(True)
    assert puppy.call_args.kwargs["noise_reduction"] is True


def test_default_robot_keeps_reply_only_speaker_behavior(robot, mocker):
    module, _, _, model, puppy = robot
    mocker.patch.object(module.config, "puppy_enabled", False)
    module.configure_reply_speaker(True)
    model.assert_not_called()
    puppy.assert_not_called()


def test_status_reports_old_profile_and_murmur_requires_running_helper(robot):
    module, _, _, _, _ = robot
    result = module.reply_speaker_status()
    assert result["puppy_configured"] is True
    assert result["puppy"] is None
    assert result["microphone"] is False
    assert result["camera_age_s"] is None
    with pytest.raises(RuntimeError, match="enable Go2 speaker"):
        module.configure_puppy_murmur(True)


def test_murmur_toggle_does_not_reload_whisper_or_stop_microphone(robot, mocker):
    module, _, _, _, puppy = robot
    mocker.patch.object(module, "_switch_puppy_audio")
    module.configure_reply_speaker(True)
    module.configure_puppy_murmur(False)
    puppy.return_value.configure_murmur.assert_called_once_with(False)
    puppy.return_value.stop.assert_not_called()


def test_failed_whisper_load_never_enables_microphone_or_speaker(robot, mocker):
    module, _, speaker, model, _ = robot
    switch = mocker.patch.object(module, "_switch_puppy_audio")
    model.side_effect = RuntimeError("Local model unavailable")
    with pytest.raises(RuntimeError, match="Local model unavailable"):
        module.configure_reply_speaker(True)
    switch.assert_not_called()
    speaker.configure.assert_not_called()


def test_pause_failure_still_turns_off_microphone_and_conversation(robot, mocker):
    module, _, speaker, _, puppy = robot
    switch = mocker.patch.object(module, "_switch_puppy_audio")
    module.configure_reply_speaker(True)
    speaker.configure.side_effect = RuntimeError("Robot pause API failed")
    with pytest.raises(RuntimeError, match="pause API failed"):
        module.configure_reply_speaker(False)
    assert switch.call_args.args == (False,)
    puppy.return_value.stop.assert_called_once()
    assert module._puppy is None


def test_sdk_audio_callback_resamples_robot_audio_and_is_removed_on_disable(robot, mocker):
    module, connection, _, _, _ = robot
    puppy = mocker.patch.object(module, "_puppy")
    loop = asyncio.new_event_loop()
    ready = Event()

    def run_loop():
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = Thread(target=run_loop)
    connection.loop = loop
    channel = connection.conn.audio
    channel.add_track_callback.side_effect = channel.track_callbacks.append
    thread.start()
    try:
        assert ready.wait(2)
        module._switch_puppy_audio(True)
        assert len(channel.track_callbacks) == 1
        frame = AudioFrame.from_ndarray(
            np.full((1, 4800), 2000, dtype=np.int16), format="s16", layout="mono"
        )
        frame.sample_rate = 48000
        asyncio.run_coroutine_threadsafe(channel.track_callbacks[0](frame), loop).result(timeout=2)
        samples = puppy.receive_pcm.call_args.args[0]
        assert samples.dtype == np.int16
        assert samples.shape[0] == 1
        assert 1500 < samples.shape[1] <= 1600
        module._switch_puppy_audio(False)
        assert channel.track_callbacks == []
        assert channel.switchAudioChannel.call_args.args == (False,)
    finally:
        mocker.patch.object(module, "_puppy", None)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2)
        loop.close()
