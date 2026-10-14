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

from dimos.agents.skills.sit_observer import SitObserver


@pytest.fixture
def observer(mocker):
    module = SitObserver()
    connection = mocker.Mock()
    connection.sit.return_value = True
    connection.rise_sit.return_value = True
    connection.recovery_stand.return_value = True
    module._connection = connection
    spoken: list[str] = []
    module._speak = lambda text, blocking: spoken.append(text)
    module.start()
    yield module, connection, spoken
    module.stop()
    module._close_module()


def test_sit_sits_and_announces(observer):
    module, connection, spoken = observer
    assert module.is_sitting is False
    assert module.sit()
    assert connection.sit.call_count == 1
    assert module.is_sitting is True
    assert spoken  # a cute line was spoken


def test_rise_sit_rises_and_recovers(observer):
    module, connection, spoken = observer
    assert module.sit()
    module.rise_sit()
    assert module.is_sitting is False
    assert connection.rise_sit.call_count == 1
    assert connection.recovery_stand.call_count == 1


def test_sit_to_observe_sits_then_rises(observer):
    module, connection, spoken = observer
    # Use a short dwell so the real sleep is instant; no need to patch.
    assert module.sit_to_observe(0.01)
    assert connection.sit.call_count == 1
    assert connection.rise_sit.call_count == 1
    assert connection.recovery_stand.call_count == 1
    assert module.is_sitting is False


def test_sit_to_observe_reports_failure_without_rising(observer):
    module, connection, _ = observer
    connection.sit.return_value = False
    assert "Could not sit" in module.sit_to_observe(0.01)
    assert connection.rise_sit.call_count == 0
    assert module.is_sitting is False


def test_recovery_stand_skipped_when_configured_off(observer):
    module, connection, _ = observer
    module.config.recovery_stand_after_sit = False
    module.sit()
    module.rise_sit()
    assert connection.recovery_stand.call_count == 0
