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

"""Which robot stacks the console can launch, and what each one provides.

The console used to launch one blueprint, ``unitree-go2-agentic-persistent-demo``,
and assumed everything that blueprint has: a persistent map, an agent and its MCP
server. A profile names a blueprint from a fixed list (the browser never supplies a
blueprint name) and says which of those the stack really has, so the console can
start it, tell when it is ready, stop it and show only the controls that apply.
"""

from dataclasses import dataclass
from typing import Literal

Connection = Literal["robot", "replay", "simulation"]

ALL_CONNECTIONS: tuple[Connection, ...] = ("robot", "replay", "simulation")


@dataclass(frozen=True)
class StackProfile:
    key: str
    label: str
    # Registry name given to ``dimos run``.
    blueprint: str
    description: str
    connections: tuple[Connection, ...]
    # PersistentGo2Map is present: map mode, alignment, saving the map on stop.
    has_map: bool = False
    # An MCP server and agent are present: chat, skills and their buttons.
    has_agent: bool = False
    # MCP tools that must be listed before an agent stack counts as ready. Stacks
    # without an agent are ready when the blueprint reports it has started.
    ready_tools: frozenset[str] = frozenset()
    # Control-deck operations that work in this stack; None means all of them.
    operations: frozenset[str] | None = frozenset()

    def allows(self, operation: str) -> bool:
        return self.operations is None or operation in self.operations

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "connections": list(self.connections),
            "has_map": self.has_map,
            "has_agent": self.has_agent,
        }


DEFAULT_PROFILE = "persistent"

PROFILES: dict[str, StackProfile] = {
    profile.key: profile
    for profile in (
        StackProfile(
            key="persistent",
            label="Persistent map + agent",
            blueprint="unitree-go2-agentic-persistent-demo",
            description="Save and restore a map, align to it, tag, navigate and explore, "
            "driven from chat or the control deck.",
            connections=ALL_CONNECTIONS,
            has_map=True,
            has_agent=True,
            ready_tools=frozenset({"tag_object", "query_memory_tags", "stop_navigation"}),
            operations=None,
        ),
        StackProfile(
            key="go2",
            label="Go2 (unitree-go2)",
            blueprint="unitree-go2",
            description="The plain Go2 stack: mapping, planner and exploration, with no agent. "
            "Click goals in the 3D view or drive with the keyboard.",
            connections=ALL_CONNECTIONS,
        ),
        StackProfile(
            key="dynamic-goal",
            label="Dynamic goal (demo)",
            blueprint="demo-unitree-go2-dynamic-goal",
            description="The planner's goal follows a person walking a loop in the MuJoCo office.",
            connections=("simulation",),
        ),
        StackProfile(
            key="person-following",
            label="Person following + agent (demo)",
            blueprint="demo-unitree-go2-agentic-person-following",
            description="An agent that tags, goes to and follows one of two people it picks by "
            "what they wear, in the MuJoCo office.",
            connections=("simulation",),
            has_agent=True,
            ready_tools=frozenset(
                {"describe_visible_people", "follow_person_with_planner", "stop_navigation"}
            ),
            operations=frozenset(
                {
                    "tag_object",
                    "tag_location",
                    "query_memory_tags",
                    "navigate_to_memory_tag",
                    "query_starting_location",
                    "return_to_starting_location",
                    "stop_navigation",
                }
            ),
        ),
    )
}
