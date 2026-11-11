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

"""Every scene kind has a colour, or loading that family raises KeyError."""

from typing import get_args

from dimos.msgs.sim_msgs.Contacts import Kind
from dimos.simulation.scenes.procedural import FAMILIES, generate
from dimos.simulation.sim_world.world import BOX_RGBA, FLOOR_CHECKER


def test_every_kind_but_the_floor_has_a_colour() -> None:
    # the floor is drawn as a checkerboard, so it is the one kind BOX_RGBA need not name
    missing = {kind for kind in get_args(Kind) if kind != "floor"} - set(BOX_RGBA)
    assert not missing, f"BOX_RGBA is missing {sorted(missing)}: that family will not load"
    assert len(FLOOR_CHECKER) == 2


def test_every_family_loads_its_boxes_into_the_world_palette() -> None:
    """The families the world can be told to build all survive the colouring step."""
    for family in FAMILIES:
        scene = generate(family, 3)
        uncoloured = [
            box.kind for box in scene.boxes if box.kind != "floor" and box.kind not in BOX_RGBA
        ]
        assert not uncoloured, f"{family} draws {sorted(set(uncoloured))} with no palette entry"
