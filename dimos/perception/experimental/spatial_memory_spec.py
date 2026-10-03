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

from dimos.msgs.sensor_msgs.Image import Image
from dimos.spec.utils import Spec
from dimos.types.robot_location import RobotLocation


class SpatialMemorySpec(Spec, Protocol):
    def capture_object_observation(self) -> tuple[Image, dict[str, Any]]: ...
    def tag_object_from_observation(
        self, name: str, bbox: list[int], image: Image, context: dict[str, Any]
    ) -> str: ...
    def tag_location(self, robot_location: RobotLocation) -> bool: ...
    def query_tagged_location(self, query: str) -> RobotLocation | None: ...
    def find_robot_location(self, name: str) -> RobotLocation | None: ...
    def query_by_text(self, text: str, limit: int = 5) -> list[dict]: ...  # type: ignore[type-arg]
    def get_robot_locations(self) -> list[RobotLocation]: ...
    def get_stats(self) -> dict[str, int]: ...
