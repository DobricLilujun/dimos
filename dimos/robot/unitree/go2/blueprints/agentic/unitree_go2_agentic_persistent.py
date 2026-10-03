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

from dataclasses import replace

from dimos.agents.skills.person_follow import PersonFollowSkillContainer
from dimos.core.coordination.blueprints import Blueprint, autoconnect
from dimos.mapping.relocalization.go2.persistent import PersistentGo2Map, PersistentGo2Planner
from dimos.mapping.voxels.module import VoxelGridMapper
from dimos.navigation.go2.replanning_a_star.module import ReplanningAStarPlanner
from dimos.navigation.movement_manager.movement_manager import MovementManager
from dimos.robot.unitree.go2.blueprints.agentic.unitree_go2_agentic import unitree_go2_agentic
from dimos.robot.unitree.go2.connection import GO2Connection
from dimos.visualization.rerun.bridge import RerunBridgeModule

_persistent = (
    autoconnect(
        unitree_go2_agentic,
        PersistentGo2Map.blueprint(),
        PersistentGo2Planner.blueprint(),
    )
    .disabled_modules(VoxelGridMapper, ReplanningAStarPlanner)
    .remappings(
        [
            (GO2Connection, "lidar", "session_lidar"),
            (GO2Connection, "odom", "session_odom"),
            (GO2Connection, "tf", "session_tf"),
            (MovementManager, "cmd_vel", "session_cmd_vel"),
            (PersonFollowSkillContainer, "cmd_vel", "session_cmd_vel"),
        ]
    )
)


def _aligned_viewer(blueprint: Blueprint) -> Blueprint:
    # Rerun consumes every TFMessage, regardless of topic. Keep raw session TF
    # out of its subscription so it cannot overwrite the aligned robot pose.
    raw_topics = {"session_lidar", "session_odom", "session_tf"}
    topics = sorted(
        {
            name
            for atom in blueprint.active_blueprints
            for port in atom.streams
            if port.direction == "out"
            and isinstance(
                name := blueprint.remapping_map.get((atom.name, port.name), port.name), str
            )
            and name not in raw_topics
        }
    )
    return replace(
        blueprint,
        blueprints=tuple(
            replace(atom, kwargs={**atom.kwargs, "topics": topics})
            if atom.module is RerunBridgeModule
            else atom
            for atom in blueprint.blueprints
        ),
    )


unitree_go2_agentic_persistent = _aligned_viewer(_persistent).global_config(n_workers=8)
