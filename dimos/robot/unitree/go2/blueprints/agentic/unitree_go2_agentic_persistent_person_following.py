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

"""The persistent Go2 stack with person tagging, going to and following.

Everything in ``unitree-go2-agentic-persistent-demo`` plus the person skills of
``demo-unitree-go2-agentic-person-following``: the agent picks a person by what they
wear and tags them, walks to them, or follows them with the planner.

Goals go through ``PersistentGo2Planner``, so they are refused until the map alignment
is approved, exactly like every other navigation. The skill's turn-on-the-spot search
publishes to ``session_cmd_vel`` (as ``MovementManager`` does) so the persistent map
sees and gates it instead of the command reaching the robot directly.
"""

from dimos.agents.mcp.mcp_client import McpClient
from dimos.agents.skills.person_follow import PersonFollowSkillContainer
from dimos.agents.skills.person_navigation import (
    PERSON_NAVIGATION_PROMPT,
    PersonNavigationSkillContainer,
)
from dimos.agents.system_prompt import SYSTEM_PROMPT
from dimos.core.coordination.blueprints import autoconnect
from dimos.mapping.relocalization.go2.persistent import PersistentGo2Planner
from dimos.navigation.experimental.frontier_exploration.wavefront_frontier_goal_selector import (
    WavefrontFrontierExplorer,
)
from dimos.navigation.go2.replanning_a_star.goal_tracker import GoalTracker
from dimos.robot.unitree.go2.blueprints.agentic.unitree_go2_agentic_persistent import (
    unitree_go2_agentic_persistent,
)

unitree_go2_agentic_persistent_person_following = (
    autoconnect(
        unitree_go2_agentic_persistent,
        PersistentGo2Planner.blueprint(navigation_speed_limit=0.55),
        # Off until the follow skill starts it; the skill supplies the targets.
        GoalTracker.blueprint(),
        PersonNavigationSkillContainer.blueprint(),
        McpClient.blueprint(system_prompt=SYSTEM_PROMPT + PERSON_NAVIGATION_PROMPT),
    )
    # follow_person drives cmd_vel directly with no obstacle avoidance and needs a Qwen
    # key; the planner-based skill replaces it.
    # Following a person drives cmd_vel directly; the stack's own frontier
    # exploration would fight it for the same stream.
    .disabled_modules(WavefrontFrontierExplorer, PersonFollowSkillContainer)
    .remappings([(PersonNavigationSkillContainer, "cmd_vel", "session_cmd_vel")])
    .global_config(n_workers=10)
)
