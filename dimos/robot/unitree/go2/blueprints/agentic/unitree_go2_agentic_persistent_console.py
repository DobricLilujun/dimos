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

"""The Go2 agentic *persistent* workflow (demo explorer variant).

Same stack as :data:`unitree_go2_agentic_persistent`, plus the demo explorer
and a persistent Go2 planner. This is the base stack the person-following
variant builds on.
"""

from dimos.core.coordination.blueprints import autoconnect
from dimos.mapping.relocalization.go2.persistent import PersistentGo2Planner
from dimos.navigation.experimental.frontier_exploration.demo_explorer import DemoExplorer
from dimos.navigation.experimental.frontier_exploration.wavefront_frontier_goal_selector import (
    WavefrontFrontierExplorer,
)
from dimos.robot.unitree.go2.blueprints.agentic.unitree_go2_agentic_persistent import (
    unitree_go2_agentic_persistent,
)

unitree_go2_agentic_persistent_demo = (
    autoconnect(
        unitree_go2_agentic_persistent,
        DemoExplorer.blueprint(),
        PersistentGo2Planner.blueprint(navigation_speed_limit=0.55),
    )
    .disabled_modules(WavefrontFrontierExplorer)
    .global_config(n_workers=8)
)
