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

import json

import numpy as np
import pytest

from dimos.msgs.sensor_msgs.Image import Image
from dimos.perception.experimental import spatial_perception
from dimos.perception.experimental.spatial_perception import SpatialMemory
from dimos.perception.experimental.spatial_vector_db import SpatialVectorDB
from dimos.perception.experimental.visual_memory import VisualMemory
from dimos.types.robot_location import RobotLocation


@pytest.fixture
def tag_store(mocker):
    client = mocker.Mock()
    database = SpatialVectorDB(chroma_client=client, visual_memory=mocker.Mock())
    return database


def test_persisted_tag_inventory_restores_duplicates_and_source_metadata(tag_store):
    first = RobotLocation(
        "fire extinguisher",
        (1, 2, 3),
        (0, 0, 0),
        location_id="first",
        metadata={"kind": "object", "description": "Near door"},
    )
    second = RobotLocation(
        "fire extinguisher",
        (5, 6, 3),
        (0, 0, 0),
        location_id="second",
    )
    tag_store.location_collection.get.return_value = {
        "metadatas": [first.to_vector_metadata(), second.to_vector_metadata()]
    }
    restored = tag_store.get_robot_locations()
    assert [tag.location_id for tag in restored] == ["first", "second"]
    assert restored[0].position == (1, 2, 3)
    assert restored[0].metadata == {"kind": "object", "description": "Near door"}


@pytest.fixture
def memory(mocker, tmp_path, tag_store):
    mocker.patch.object(spatial_perception, "ImageEmbeddingProvider")
    mocker.patch.object(spatial_perception, "SpatialVectorDB", return_value=tag_store)
    module = SpatialMemory(
        chroma_client=mocker.Mock(),
        visual_memory=VisualMemory(output_dir=str(tmp_path)),
        db_path=None,
        output_dir=str(tmp_path),
        vlm_url=None,
    )
    yield module
    module.dispose()


@pytest.mark.parametrize("distance,expected_adds", [(0.9, 0), (1.0, 0), (1.1, 1)])
def test_object_tag_merges_only_nearby_same_name(memory, mocker, distance, expected_adds):
    existing = RobotLocation("fire extinguisher", (0, 0, 0), (0, 0, 0))
    mocker.patch.object(memory, "get_robot_locations", return_value=[existing])
    add = mocker.patch.object(memory, "add_named_location", return_value=True)
    assert memory._tag_object_location(
        "Fire Extinguisher", [distance, 0, 0], [0, 0, 0], "Seen near door"
    )
    assert add.call_count == expected_adds


def test_memory_inventory_reads_saved_database_not_session_list(memory, tag_store):
    stored = RobotLocation("fire extinguisher", (1, 2, 3), (0, 0, 0))
    tag_store.location_collection.get.return_value = {"metadatas": [stored.to_vector_metadata()]}
    assert memory.get_robot_locations()[0].location_id == stored.location_id
    assert memory.find_robot_location("Fire Extinguisher").position == (1, 2, 3)


def test_object_tag_does_not_merge_different_names_at_same_position(memory, mocker):
    mocker.patch.object(
        memory,
        "get_robot_locations",
        return_value=[RobotLocation("chair", (0, 0, 0), (0, 0, 0))],
    )
    add = mocker.patch.object(memory, "add_named_location", return_value=True)
    assert memory._tag_object_location("fire extinguisher", [0, 0, 0], [0, 0, 0], "Near door")
    add.assert_called_once_with(
        "fire extinguisher", [0, 0, 0], [0, 0, 0], "Near door", kind="object"
    )


def test_failed_tag_write_is_not_reported_as_saved(memory, tag_store):
    tag_store.location_collection.add.side_effect = RuntimeError("database unavailable")
    location = RobotLocation("fire extinguisher", (1, 2, 3), (0, 0, 0))
    assert not memory.add_robot_location(location)
    assert memory.robot_locations == []


@pytest.fixture
def geometry():
    rotation = np.array([[0, 0, 1], [0, 1, 0], [-1, 0, 0]])
    origin = np.array([10, 20, 1])
    points = np.array([[0, 0, 4], [0.1, 0, 4], [-0.1, 0, 4], [2, 0, 1]])
    return {
        "image_size": (100, 100),
        "intrinsics": [[100, 0, 50], [0, 100, 50], [0, 0, 1]],
        "camera_origin": origin,
        "camera_rotation": rotation,
        "world_points": points @ rotation.T + origin,
    }


def test_tagged_object_is_projected_using_captured_pose_and_lidar(memory, geometry, mocker):
    mocker.patch.object(memory, "get_robot_locations", return_value=[])
    add = mocker.patch.object(memory, "add_named_location", return_value=True)
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    mocker.patch.object(memory, "_latest_observation", (frame, geometry, 10.0))
    image, context = memory.capture_object_observation()
    # The live pose/sensors can change while the model detects the box.
    mocker.patch.object(memory, "_latest_observation", None)
    result = json.loads(
        memory.tag_object_from_observation("fire extinguisher", [40, 40, 60, 60], image, context)
    )
    assert result["position"] == [14, 20, 1]
    assert result["point_count"] == 3
    assert result["method"] == "pointcloud_bbox"
    assert add.call_args.args[1] == [14, 20, 1]
    assert add.call_args.kwargs["kind"] == "object"


def test_object_without_lidar_hit_is_not_tagged_at_default_distance(memory, geometry, mocker):
    geometry["world_points"] = np.empty((0, 3))
    add = mocker.patch.object(memory, "_tag_object_location")
    image = Image.from_numpy(np.zeros((100, 100, 3), dtype=np.uint8))
    with pytest.raises(RuntimeError, match="no valid object depth"):
        memory.tag_object_from_observation("fire extinguisher", [40, 40, 60, 60], image, geometry)
    add.assert_not_called()


def test_segmentation_lidar_position_uses_foreground_not_box_center(memory, geometry, mocker):
    # Foreground is off the box centre; a nearby background point must be excluded.
    geometry["camera_rotation"] = np.eye(3)
    geometry["camera_origin"] = np.array([10, 20, 1])
    geometry["world_points"] = np.array([[10.3, 20, 5], [9.7, 20, 3]])
    mask = np.zeros((100, 100), dtype=bool)
    mask[50, 57] = True
    segmenter = mocker.patch.object(memory, "_object_segmenter")
    segmenter.segment.return_value = mask
    estimate = memory._estimate_target(
        [40, 40, 60, 60],
        "fire extinguisher",
        np.zeros((100, 100, 3), dtype=np.uint8),
        geometry,
        require_depth=True,
    )
    np.testing.assert_allclose(estimate["position"], [10.3, 20, 5])
    assert estimate["point_count"] == 1
