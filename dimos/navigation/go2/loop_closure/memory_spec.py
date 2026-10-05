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

from typing import Any, Protocol

from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.sensor_msgs.PointCloud2 import PointCloud2
from dimos.navigation.go2.loop_closure.pgo import PoseGraph
from dimos.spec.utils import Spec


class PGOMemorySpec(Spec, Protocol):
    def update_pgo_graph(
        self, graph: PoseGraph, session_id: str, map_cloud: PointCloud2 | None = None
    ) -> None: ...


class PGONavigationSpec(Spec, Protocol):
    def pause_for_pgo(self) -> int: ...
    def resume_after_pgo(
        self, revision: int, map_cloud: PointCloud2, odom: PoseStamped | None
    ) -> bool: ...


class TaggedNavigationSpec(Spec, Protocol):
    def set_tagged_goal(self, location_id: str, goal: PoseStamped) -> bool: ...


class NearbyNavigationSpec(Spec, Protocol):
    def set_nearby_tagged_goal(self, location_id: str, goal: PoseStamped) -> bool: ...


class TagViewSpec(Spec, Protocol):
    def verify_tag_view(self, location_id: str) -> dict[str, Any]: ...
