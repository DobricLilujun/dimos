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

from threading import Event, Thread

import pytest

from dimos.core.global_config import GlobalConfig
from dimos.navigation.go2.replanning_a_star.local_planner import LocalPlanner
from dimos.navigation.go2.replanning_a_star.navigation_map import NavigationMap


@pytest.fixture
def planner():
    config = GlobalConfig()
    local = LocalPlanner(config, NavigationMap(config, "voronoi"), 0.2)
    yield local
    local.stop()


def test_stop_waits_for_previous_navigation_thread_before_replanning(planner, mocker):
    entered = Event()
    exited = Event()

    def controlled_loop():
        entered.set()
        assert planner._stop_planning_event.wait(2)
        exited.set()

    mocker.patch.object(planner, "_loop", side_effect=controlled_loop)
    thread = Thread(target=planner._thread_entrypoint)
    mocker.patch.object(planner, "_thread", thread)
    thread.start()
    try:
        assert entered.wait(2)
        planner.stop_planning()
        assert exited.is_set()
        assert not thread.is_alive()
        assert planner._thread is None
    finally:
        planner._stop_planning_event.set()
        thread.join(2)


def test_stop_timeout_refuses_new_navigation_instead_of_overlapping_threads(planner, mocker):
    thread = mocker.Mock()
    thread.is_alive.return_value = True
    mocker.patch.object(planner, "_thread", thread)
    with pytest.raises(RuntimeError, match="thread did not stop"):
        planner.stop_planning()
    thread.join.assert_called_once()
    assert planner._thread is thread
    thread.is_alive.return_value = False
