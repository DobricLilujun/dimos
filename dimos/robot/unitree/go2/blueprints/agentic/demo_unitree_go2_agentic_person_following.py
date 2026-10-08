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

"""An agent that tags, walks to and follows a person picked out by what they wear.

    dimos --simulation run demo-unitree-go2-agentic-person-following
    dimos agent-send "follow the person in the white t-shirt"

Two people walk loops in the MuJoCo office: one in a dark top and beige trousers, one
in a white t-shirt and dark trousers. The agent picks one by appearance and follows
them with the A* planner, re-planning as they move.
"""

from dimos.agents.mcp.mcp_client import McpClient
from dimos.agents.skills.person_follow import PersonFollowSkillContainer
from dimos.agents.skills.person_navigation import (
    PERSON_NAVIGATION_PROMPT,
    PersonNavigationSkillContainer,
)
from dimos.agents.system_prompt import SYSTEM_PROMPT
from dimos.core.coordination.blueprints import autoconnect
from dimos.navigation.go2.replanning_a_star.goal_tracker import GoalTracker
from dimos.robot.unitree.go2.blueprints.agentic.unitree_go2_agentic import unitree_go2_agentic
from dimos.simulation.mujoco.person_target import DEFAULT_SECOND_TRACK, MujocoPersonTarget

demo_unitree_go2_agentic_person_following = (
    autoconnect(
        unitree_go2_agentic,
        # Off until the follow skill starts it; the skill supplies the targets.
        GoalTracker.blueprint(update_threshold_m=0.5, follow_distance_m=0.5),
        PersonNavigationSkillContainer.blueprint(),
        # The agent finds people through the camera, so the simulator's ground truth
        # is not published as a target.
        MujocoPersonTarget.blueprint(
            publish_target=False, second_person_track=DEFAULT_SECOND_TRACK
        ),
        McpClient.blueprint(system_prompt=SYSTEM_PROMPT + PERSON_NAVIGATION_PROMPT),
    )
    # follow_person drives cmd_vel directly with no obstacle avoidance and needs a Qwen
    # key; the planner-based skill replaces it.
    .disabled_modules(PersonFollowSkillContainer)
    .global_config(
        n_workers=10,
        robot_model="unitree_go2",
        mujoco_start_pos="-6.18 0.96",
        mujoco_second_person=True,
    )
)
