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

import xml.etree.ElementTree as ET

import cv2
import numpy as np
import pytest

pytest.importorskip("mujoco_playground")

from dimos.msgs.geometry_msgs.Pose import Pose
from dimos.simulation.mujoco import model as mujoco_model
from dimos.simulation.mujoco.person_on_track import PersonPositionController

SCENE = "<mujoco><asset/><worldbody/></mujoco>"


def _people(xml: str) -> dict[str, ET.Element]:
    root = ET.fromstring(xml)
    return {body.get("name", ""): body for body in root.iter("body")}


def test_there_is_one_person_unless_a_second_is_requested() -> None:
    xml = mujoco_model.get_model_xml("unitree_go1", SCENE)

    assert set(_people(xml)) == {"person"}
    root = ET.fromstring(xml)
    assert [t.get("file") for t in root.iter("texture")] == ["material_0.png"]


def test_second_person_shares_the_mesh_but_has_its_own_texture_and_mocap_body() -> None:
    xml = mujoco_model.get_model_xml("unitree_go1", SCENE, second_person=True)

    people = _people(xml)
    assert set(people) == {"person", "person2"}
    assert all(body.get("mocap") == "true" for body in people.values())
    root = ET.fromstring(xml)
    assert [m.get("name") for m in root.iter("mesh")] == ["person_mesh"]
    textures = {t.get("name"): t.get("file") for t in root.iter("texture")}
    assert textures == {"person_texture": "material_0.png", "person2_texture": "material_1.png"}
    materials = {m.get("name"): m.get("texture") for m in root.iter("material")}
    assert materials == {
        "person_material": "person_texture",
        "person2_material": "person2_texture",
    }
    geoms = {name: body.find("geom") for name, body in people.items()}
    assert geoms["person"].get("material") == "person_material"  # type: ignore[union-attr]
    assert geoms["person2"].get("material") == "person2_material"  # type: ignore[union-attr]
    assert geoms["person2"].get("mesh") == "person_mesh"  # type: ignore[union-attr]


@pytest.fixture
def person_assets(mocker, tmp_path):  # type: ignore[no-untyped-def]
    ok, encoded = cv2.imencode(".png", np.full((4, 4, 3), (80, 40, 30), np.uint8))
    assert ok
    original = bytes(encoded.tobytes())
    person_dir = tmp_path / "person"

    def update_assets(assets, path, pattern=None):  # type: ignore[no-untyped-def]
        if path == person_dir and pattern == "*.png":
            assets["material_0.png"] = original

    mocker.patch.object(mujoco_model, "mjx_env")
    mujoco_model.mjx_env.update_assets.side_effect = update_assets
    mocker.patch.object(mujoco_model, "_get_data_dir", return_value=tmp_path)
    mocker.patch.object(mujoco_model, "get_data", return_value=person_dir)
    return original


def test_assets_only_gain_the_recolored_texture_when_asked(person_assets: bytes) -> None:
    assert "material_1.png" not in mujoco_model.get_assets()

    assets = mujoco_model.get_assets(second_person=True)

    assert assets["material_0.png"] == person_assets
    assert assets["material_1.png"] != person_assets


def _two_mocap_model():  # type: ignore[no-untyped-def]
    import mujoco

    return mujoco.MjModel.from_xml_string(
        """
        <mujoco><worldbody>
          <body name="person" mocap="true"><geom type="sphere" size="0.1"/></body>
          <body name="person2" mocap="true"><geom type="sphere" size="0.1"/></body>
        </worldbody></mujoco>
        """
    )


def test_each_controller_moves_only_its_own_body(mocker) -> None:  # type: ignore[no-untyped-def]
    import mujoco

    transports: dict[str, object] = {}
    callbacks: dict[str, object] = {}

    def make_transport(topic, _type):  # type: ignore[no-untyped-def]
        transport = mocker.Mock()
        transport.subscribe.side_effect = lambda cb, topic=topic: callbacks.__setitem__(topic, cb)
        transports[topic] = transport
        return transport

    mocker.patch("dimos.simulation.mujoco.person_on_track.make_transport", make_transport)
    model = _two_mocap_model()
    data = mujoco.MjData(model)
    first = PersonPositionController(model)
    second = PersonPositionController(model, body_name="person2", topic="/person2_pose")

    assert set(transports) == {"/person_pose", "/person2_pose"}
    callbacks["/person2_pose"](Pose(position=[4.0, 5.0, 0.0], orientation=[0.0, 0.0, 0.0, 1.0]))  # type: ignore[operator]
    first.tick(data)
    second.tick(data)

    first_id = model.body_mocapid[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "person")]
    second_id = model.body_mocapid[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "person2")]
    assert list(data.mocap_pos[second_id][:2]) == [4.0, 5.0]
    assert list(data.mocap_pos[first_id][:2]) == [0.0, 0.0]
