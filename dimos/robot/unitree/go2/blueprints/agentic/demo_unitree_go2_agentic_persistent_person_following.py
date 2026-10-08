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

"""``unitree-go2-agentic-persistent-person-following`` in the MuJoCo office.

Adds two people walking loops, as in ``demo-unitree-go2-agentic-person-following``:
one in a dark top and beige trousers, one in a white top and dark trousers. The agent
finds them through the camera, so the simulator's own positions are not published.
Build a new map first; a saved real-lidar map cannot be restored in simulation.

    dimos --simulation run demo-unitree-go2-agentic-persistent-person-following \\
        --persistentgo2map.map-file=/tmp/sim_scene/map.pc2.lcm \\
        --persistentgo2map.create-new=true --spatialmemory.scene-map-dir=/tmp/sim_scene
"""

from dimos.core.coordination.blueprints import autoconnect
from dimos.robot.unitree.go2.blueprints.agentic.unitree_go2_agentic_persistent_person_following import (
    unitree_go2_agentic_persistent_person_following,
)
from dimos.simulation.mujoco.person_target import DEFAULT_SECOND_TRACK, MujocoPersonTarget

demo_unitree_go2_agentic_persistent_person_following = autoconnect(
    unitree_go2_agentic_persistent_person_following,
    MujocoPersonTarget.blueprint(publish_target=False, second_person_track=DEFAULT_SECOND_TRACK),
).global_config(
    n_workers=11,
    mujoco_start_pos="-6.18 0.96",
    mujoco_second_person=True,
    # People stand 3-6 m away at the start; the default 3 m lidar would not see them.
    mujoco_lidar_max_range=8.0,
)
