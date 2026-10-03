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

"""Fast unit tests for SceneGraphServerModule.

A real (tiny) HTTP server is started on an ephemeral port and queried with
urllib; the scene graph (SpatialMemory) is a fake. No heavy deps.
"""

import json
import time
import urllib.request

import pytest

from dimos.agents.skills.scene_graph_server import SceneGraphServerModule
from dimos.types.robot_location import RobotLocation


# -- fakes -------------------------------------------------------------------
class FakeMemory:
    def __init__(self) -> None:
        self._queries: list[str] = []

    def get_robot_locations(self) -> list[RobotLocation]:
        return [RobotLocation(name="bed", position=(-3.5, -1.3, 0.1), rotation=(0.0, 0.0, 0.0))]

    def get_stats(self) -> dict[str, int]:
        return {"frames": 120, "locations": 1}

    def query_by_text(self, text: str, limit: int = 5) -> list[dict]:
        self._queries.append(text)
        if "bed" in text:
            return [{"distance": 0.3, "metadata": [{"pos_x": -3.5, "pos_y": -1.3}]}]
        return []

    def tag_location(self, loc) -> bool:
        return True

    def query_tagged_location(self, query: str):
        return None


_created: list[SceneGraphServerModule] = []


@pytest.fixture(autouse=True)
def _stop_created_modules() -> None:
    yield
    for m in _created:
        try:
            m.stop()
        except Exception:
            pass
    _created.clear()


def make(port: int, **config) -> SceneGraphServerModule:
    m = SceneGraphServerModule(port=port, **config)
    _created.append(m)
    return m


def _get_json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.load(resp)


# -- tests -------------------------------------------------------------------
def test_noop_when_no_port() -> None:
    """With port=None the module starts without a server (no-op)."""
    m = make(None)
    m._spatial_memory = FakeMemory()
    m.start()
    assert m._server is None
    m.stop()


def test_scene_graph_endpoint() -> None:
    m = make(5591)
    m._spatial_memory = FakeMemory()
    m.start()
    time.sleep(0.3)
    try:
        data = _get_json("http://127.0.0.1:5591/scene_graph")
        assert data["stats"] == {"frames": 120, "locations": 1}
        assert data["locations"][0]["name"] == "bed"
        assert data["locations"][0]["position"] == [-3.5, -1.3, 0.1]
    finally:
        m.stop()


def test_query_endpoint() -> None:
    m = make(5592)
    memory = FakeMemory()
    m._spatial_memory = memory
    m.start()
    time.sleep(0.3)
    try:
        data = _get_json("http://127.0.0.1:5592/query?q=the+bed")
        assert data["query"] == "the bed"
        assert len(data["results"]) == 1
        assert data["results"][0]["distance"] == 0.3
        # the query reached the memory
        assert "the bed" in memory._queries
    finally:
        m.stop()


def test_query_endpoint_no_match() -> None:
    m = make(5593)
    m._spatial_memory = FakeMemory()
    m.start()
    time.sleep(0.3)
    try:
        data = _get_json("http://127.0.0.1:5593/query?q=nothing")
        assert data["results"] == []
    finally:
        m.stop()


def test_index_is_html() -> None:
    m = make(5594)
    m._spatial_memory = FakeMemory()
    m.start()
    time.sleep(0.3)
    try:
        html = urllib.request.urlopen("http://127.0.0.1:5594/", timeout=5).read().decode()
        assert "Scene Graph" in html
    finally:
        m.stop()
