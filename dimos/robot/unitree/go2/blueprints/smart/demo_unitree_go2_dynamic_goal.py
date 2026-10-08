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

"""Go2 following a person who walks a loop in the MuJoCo office.

    dimos --simulation run demo-unitree-go2-dynamic-goal

The robot replans to the person's new position every time they move 0.5 m, and
stops 0.5 m short of them.
"""

from dimos.core.coordination.blueprints import autoconnect
from dimos.navigation.go2.replanning_a_star.goal_tracker import GoalTracker
from dimos.robot.unitree.go2.blueprints.smart.unitree_go2 import unitree_go2
from dimos.simulation.mujoco.person_target import MujocoPersonTarget

demo_unitree_go2_dynamic_goal = autoconnect(
    unitree_go2,
    GoalTracker.blueprint(enabled=True, update_threshold_m=0.5, follow_distance_m=0.5),
    MujocoPersonTarget.blueprint(),
).global_config(n_workers=12, robot_model="unitree_go2", mujoco_start_pos="-6.18 0.96")
