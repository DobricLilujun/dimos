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

import pytest

from dimos.navigation.go2.replanning_a_star.module import ReplanningAStarPlanner


@pytest.fixture
def planner_module():
    module = ReplanningAStarPlanner()
    yield module
    module._close_module()


def test_set_planner_width_updates_live_status(planner_module):
    assert planner_module.set_planner_width(0.1) == {"width_m": 0.1}
    assert planner_module.planner_width_status() == {"width_m": 0.1}


@pytest.mark.parametrize("width", [True, 0.09, 1.01, float("nan"), float("inf")])
def test_set_planner_width_rejects_invalid_values(planner_module, width):
    with pytest.raises(ValueError, match="between 0.1 and 1.0"):
        planner_module.set_planner_width(width)

    assert planner_module.planner_width_status() == {"width_m": 0.3}
