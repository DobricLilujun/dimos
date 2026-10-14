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

import pytest

from dimos.agents.mcp import tool_stream
from dimos.agents.skills import cute_phrases
from dimos.agents.skills.mode_announcer import ModeAnnouncer


@pytest.fixture
def announcer(mocker):
    module = ModeAnnouncer()
    spoken: list[tuple[str, bool]] = []
    module._speak = lambda text, blocking: spoken.append((text, blocking))
    # Capture the callback the module subscribes with so tests can invoke it
    # directly without a live transport.
    captured = {}

    def fake_subscribe(cb):
        captured["callback"] = cb
        return lambda: None

    mocker.patch.object(tool_stream, "subscribe", side_effect=fake_subscribe)
    module.start()
    yield module, spoken, captured
    module.stop()
    module._close_module()


def _send(announcer, *frames):
    module, spoken, captured = announcer
    cb = captured["callback"]
    assert cb is not None
    for frame in frames:
        cb(frame)
    return spoken


def test_enter_speaks_cute_line_once(announcer):
    spoken = _send(announcer, tool_stream.make_notification("begin_demo_exploration", "working"))
    assert (cute_phrases.enter_phrase("begin_demo_exploration"), False) in spoken
    # A second progress frame for the same mode does not re-speak.
    spoken = _send(announcer, tool_stream.make_progress_notification(1, 50, "still exploring"))
    enter_count = sum(
        1 for s in spoken if s[0] == cute_phrases.enter_phrase("begin_demo_exploration")
    )
    assert enter_count == 1


def test_exit_speaks_exit_line_after_stop(announcer):
    _send(announcer, tool_stream.make_notification("follow_person", "tracking"))
    spoken = _send(announcer, tool_stream.make_stopped_notification("follow_person"))
    assert cute_phrases.exit_phrase("follow_person") in [s[0] for s in spoken]


def test_unknown_tool_is_silent(announcer):
    _send(announcer, tool_stream.make_notification("tag_object", "x"))
    spoken = _send(announcer, tool_stream.make_notification("not_a_mode", "x"))
    assert all("not_a_mode" not in s[0] for s in spoken)


def test_disabled_announcer_never_speaks(mocker):
    module = ModeAnnouncer()
    module.config.enabled = False
    spoken: list = []
    module._speak = lambda text, blocking: spoken.append((text, blocking))
    mocker.patch.object(tool_stream, "subscribe", return_value=lambda: None)
    module.start()
    module.stop()
    assert spoken == []


def test_phrase_table_has_enter_and_exit(mocker):
    assert cute_phrases.enter_phrase("begin_demo_exploration")
    assert cute_phrases.exit_phrase("end_demo_exploration")
    assert cute_phrases.is_mode_tool("follow_person")
    assert not cute_phrases.is_mode_tool("not_a_mode")
    assert cute_phrases.exit_phrase("not_a_mode") is None
