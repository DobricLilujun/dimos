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
import queue
import time

import numpy as np
import pytest

from dimos.msgs.geometry_msgs.Quaternion import Quaternion
from dimos.msgs.geometry_msgs.Transform import Transform
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.sensor_msgs.CameraInfo import CameraInfo
from dimos.msgs.sensor_msgs.Image import Image
from dimos.msgs.sensor_msgs.PointCloud2 import PointCloud2
from dimos.navigation.go2.loop_closure.pgo import Keyframe, PoseGraph
from dimos.perception.experimental import spatial_perception
from dimos.perception.experimental.spatial_perception import SpatialMemory
from dimos.perception.experimental.spatial_vector_db import SpatialVectorDB
from dimos.perception.experimental.visual_memory import VisualMemory
from dimos.types.robot_location import RobotLocation


@pytest.fixture
def tag_store(mocker):
    client = mocker.Mock()
    collections = [mocker.Mock(), mocker.Mock()]
    for collection in collections:
        collection.get.return_value = {"ids": [], "metadatas": []}
    client.get_or_create_collection.side_effect = collections
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
        visual_memory_path=str(tmp_path / "visual_memory.pkl"),
        output_dir=str(tmp_path),
        vlm_url=None,
    )
    yield module
    module.dispose()


def test_tag_reference_is_persisted_and_exact_id_is_used_for_matching(memory, mocker):
    frame = np.full((100, 120, 3), 100, dtype=np.uint8)
    target = RobotLocation("chair", (1, 2, 0), (0, 0, 0), location_id="chair-1")
    memory._attach_tag_image(target, frame)
    assert target.frame_id == "tag_chair-1"
    assert VisualMemory.load(memory.visual_memory_path).get("tag_chair-1").shape == frame.shape
    mocker.patch.object(memory, "get_robot_locations", return_value=[target])
    memory._latest_observation = (frame, None, time.time())
    match = mocker.patch.object(
        spatial_perception, "match_tag_view", return_value={"matched": True, "inliers": 20}
    )
    result = memory.verify_tag_view("chair-1")
    assert result["matched"] is True
    assert result["location_id"] == "chair-1"
    assert np.array_equal(match.call_args.args[0], frame)
    assert np.array_equal(match.call_args.args[1], frame)


def test_visual_search_refuses_old_tag_without_reference_and_stale_camera(memory, mocker):
    target = RobotLocation("chair", (1, 2, 0), (0, 0, 0))
    mocker.patch.object(memory, "get_robot_locations", return_value=[target])
    with pytest.raises(ValueError, match="re-tag"):
        memory.verify_tag_view(target.location_id)
    frame = np.zeros((100, 120, 3), dtype=np.uint8)
    memory._attach_tag_image(target, frame)
    memory._latest_observation = (frame, None, time.time() - 4)
    with pytest.raises(RuntimeError, match="fresh camera"):
        memory.verify_tag_view(target.location_id)


def test_object_reference_crop_is_the_object_not_the_whole_room(memory):
    frame = np.arange(300, dtype=np.uint8).reshape(10, 10, 3)
    assert np.array_equal(memory._tag_crop(frame, [2, 3, 7, 8]), frame[3:8, 2:7])


def test_retagging_existing_object_adds_missing_reference_without_changing_id(memory, mocker):
    target = RobotLocation("chair", (1, 2, 0), (0, 0, 0), location_id="old-chair")
    mocker.patch.object(memory, "get_robot_locations", return_value=[target])
    frame = np.full((100, 100, 3), 120, dtype=np.uint8)
    assert memory._tag_object_location(
        "chair", [1, 2, 0], [0, 0, 0], "Chair", reference_image=frame
    )
    assert target.frame_id == "tag_old-chair"
    memory.vector_db.location_collection.update.assert_called_once_with(
        ids=["old-chair"], metadatas=[target.to_vector_metadata()]
    )


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


def test_automatic_tagging_pause_discards_queued_and_inflight_tags(memory, mocker):
    mocker.patch.object(memory, "_vlm", mocker.Mock())
    mocker.patch.object(memory.config, "vlm_enable_place_tagging", True)
    tasks = queue.Queue()
    mocker.patch.object(memory, "_vlm_task_queue", tasks)
    tasks.put({"frame_id": "old"})
    apply = mocker.patch.object(memory, "_apply_vlm_result")

    assert memory.set_automatic_tagging(False)["enabled"] is False
    assert tasks.empty()
    memory._handle_vlm_result({"generation": 0, "place": "old"})
    assert memory.set_automatic_tagging(True)["enabled"] is True
    memory._handle_vlm_result({"generation": 0, "place": "late"})
    apply.assert_not_called()
    memory._handle_vlm_result({"generation": 2, "place": "new"})
    apply.assert_called_once_with({"generation": 2, "place": "new"})


def test_automatic_tagging_requires_configured_vlm(memory):
    with pytest.raises(RuntimeError, match="Configure a VLM"):
        memory.set_automatic_tagging(True)


def test_pgo_rewrites_only_this_runs_tag_and_image_coordinates_from_raw_baseline(
    memory, tag_store, mocker
):
    graph = PoseGraph(
        keyframes=(
            Keyframe(10, Transform(), Transform()),
            Keyframe(12, Transform(), Transform(translation=Vector3(0.5, 0, 0))),
        )
    )
    metadata = {
        "pos_x": 2.0,
        "pos_y": 0.0,
        "pos_z": 1.0,
        "rot_x": 0.0,
        "rot_y": 0.0,
        "rot_z": 0.0,
    }
    mocker.patch.object(memory, "_pgo_session", "run-one")
    original = memory._pgo_metadata(metadata, 12, None)
    for collection in (tag_store.location_collection, tag_store.image_collection):
        collection.get.return_value = {"ids": ["tag-or-image"], "metadatas": [original]}

    memory.update_pgo_graph(graph, "run-one")
    memory.update_pgo_graph(graph, "run-one")

    for collection in (tag_store.location_collection, tag_store.image_collection):
        assert collection.get.call_args.kwargs["where"] == {"pgo_session": "run-one"}
        corrected = collection.update.call_args.kwargs["metadatas"][0]
        assert corrected["pos_x"] == pytest.approx(2.5)
        assert json.loads(corrected["pgo_raw_pose"])[0][3] == 2
    with pytest.raises(RuntimeError, match="two live PGO sessions"):
        memory.update_pgo_graph(graph, "another-run")


def test_delayed_tag_is_reprojected_again_when_persisted_after_another_loop(
    memory, tag_store, mocker
):
    old_graph = PoseGraph(
        keyframes=(
            Keyframe(10, Transform(), Transform()),
            Keyframe(12, Transform(), Transform(translation=Vector3(0.5, 0, 0))),
        )
    )
    new_graph = PoseGraph(
        keyframes=(
            Keyframe(10, Transform(), Transform()),
            Keyframe(
                12, Transform(), Transform(rotation=Quaternion.from_euler(Vector3(0, 0, np.pi / 2)))
            ),
        )
    )
    mocker.patch.object(memory, "_pgo_session", "run-one")
    mocker.patch.object(memory, "_pgo_graph", old_graph)
    captured = RobotLocation("chair", (2.5, 0, 1), (0, 0, 0), timestamp=12.0)
    metadata = memory._pgo_metadata(captured.to_vector_metadata(), 12.0, old_graph)
    delayed = RobotLocation.from_vector_metadata(metadata)
    mocker.patch.object(memory, "_pgo_graph", new_graph)

    assert memory.tag_location(delayed)

    persisted = tag_store.location_collection.add.call_args.kwargs["metadatas"][0]
    np.testing.assert_allclose([persisted[f"pos_{axis}"] for axis in "xyz"], [0, 2, 1], atol=1e-8)
    assert persisted["rot_z"] == pytest.approx(np.pi / 2)
    assert json.loads(persisted["pgo_raw_pose"])[0][3] == 2


def test_pgo_capture_uses_raw_sensor_history_not_old_corrected_tf(memory, tag_store, mocker):
    graph = PoseGraph(
        keyframes=(
            Keyframe(10, Transform(), Transform()),
            Keyframe(12, Transform(), Transform(translation=Vector3(0.5, 0, 0))),
        )
    )
    for collection in (tag_store.location_collection, tag_store.image_collection):
        collection.get.return_value = {"ids": [], "metadatas": []}
    saved_map = PointCloud2.from_numpy(np.array([[2.5, 0, 1]]), frame_id="world", timestamp=12.0)
    memory.update_pgo_graph(graph, "run-one", saved_map)
    mocker.patch.object(
        memory,
        "_latest_camera_info",
        CameraInfo(
            width=100,
            height=100,
            frame_id="camera_optical",
            K=[100, 0, 50, 0, 100, 50, 0, 0, 1],
        ),
    )
    old_buffer = mocker.patch.object(memory, "_tf")
    old_buffer.get.side_effect = AssertionError("Do not reuse old corrected TF after a loop")
    memory._pgo_raw_buffer.receive_transform(
        Transform(
            translation=Vector3(1, 0, 0), frame_id="world", child_frame_id="base_link", ts=12.0
        ),
        Transform(
            translation=Vector3(0, 0, 1),
            frame_id="base_link",
            child_frame_id="camera_optical",
            ts=12.0,
        ),
    )
    mocker.patch.object(
        memory,
        "_pgo_raw_cloud",
        PointCloud2.from_numpy(np.array([[2, 0, 1]]), frame_id="world", timestamp=12.0),
    )

    context = memory._capture_projection_context(np.zeros((100, 100, 3)), 12.0)

    np.testing.assert_allclose(context["camera_origin"], [1.5, 0, 1])
    np.testing.assert_allclose(context["robot_origin"], [1.5, 0, 0])
    np.testing.assert_allclose(context["world_points"], [[2.5, 0, 1]])
    np.testing.assert_allclose(context["map_points"], [[2.5, 0, 1]])
    assert context["robot_tf"].ts == 12.0
    assert context["pgo_graph"] is graph


def test_partial_pgo_database_failure_blocks_queries_and_further_tags(memory, tag_store, mocker):
    mocker.patch.object(memory, "_pgo_session", "run-one")
    location = RobotLocation("chair", (2, 0, 1), (0, 0, 0), timestamp=12.0)
    metadata = memory._pgo_metadata(location.to_vector_metadata(), 12.0, None)
    for collection in (tag_store.location_collection, tag_store.image_collection):
        collection.get.return_value = {"ids": ["entry"], "metadatas": [metadata]}
    tag_store.image_collection.update.side_effect = RuntimeError("image database unavailable")
    graph = PoseGraph(keyframes=(Keyframe(12, Transform(), Transform()),))

    with pytest.raises(RuntimeError, match="image database unavailable"):
        memory.update_pgo_graph(graph, "run-one")

    with pytest.raises(RuntimeError, match="PGO synchronization failed"):
        memory.get_robot_locations()
    with pytest.raises(RuntimeError, match="PGO synchronization failed"):
        memory.set_automatic_tagging(True)
    assert not memory.tag_location(location)
    tag_store.location_collection.add.assert_not_called()


def test_object_tag_does_not_merge_different_names_at_same_position(memory, mocker):
    mocker.patch.object(
        memory,
        "get_robot_locations",
        return_value=[RobotLocation("chair", (0, 0, 0), (0, 0, 0))],
    )
    add = mocker.patch.object(memory, "add_named_location", return_value=True)
    assert memory._tag_object_location("fire extinguisher", [0, 0, 0], [0, 0, 0], "Near door")
    add.assert_called_once_with(
        "fire extinguisher", [0, 0, 0], [0, 0, 0], "Near door", kind="object", reference_image=None
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
        "map_points": np.array([[9, 19, 1], [15, 19, 1], [15, 21, 1], [9, 21, 1]]),
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


@pytest.fixture
def bounded_map():
    return {
        "camera_origin": np.array([0.3, 0, 1]),
        "robot_origin": np.array([0, 0, 0]),
        "world_points": np.empty((0, 3)),
        "map_points": np.array(
            [
                [-2, -2, 1],
                [2, -2, 1],
                [2, 2, 1],
                [-2, 2, 1],
                [2, 0, 1],
                [0, 2, 1],
            ]
        ),
    }


def test_outside_object_uses_sight_line_boundary_then_thirty_cm_toward_robot(bounded_map):
    estimate = {"position": [7, 0, 1], "method": "default_distance", "point_count": 0}
    result = SpatialMemory._constrain_object_to_map(estimate, bounded_map)
    assert result["position"] == [1.7, 0, 1]
    assert result["boundary_position"] == [2, 0, 1]
    assert result["estimated_position"] == [7, 0, 1]
    assert result["method"] == "pointcloud_map_boundary_inset"
    assert result["inset_m"] == 0.3


def test_inside_object_is_not_moved(bounded_map):
    estimate = {"position": [1, 1, 1], "method": "pointcloud_bbox", "point_count": 1}
    assert SpatialMemory._constrain_object_to_map(estimate, bounded_map) == estimate


@pytest.mark.parametrize("boundary_distance", [0.2, 0.3])
def test_boundary_within_thirty_cm_does_not_inset_past_robot(bounded_map, boundary_distance):
    bounded_map["robot_origin"] = np.array([2 - boundary_distance, 0, 0])
    assert (
        SpatialMemory._constrain_object_to_map(
            {"position": [7, 0, 1], "point_count": 0}, bounded_map
        )
        is None
    )


def test_rotated_translated_map_uses_robot_direction_not_world_x_axis(bounded_map):
    rotation = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]])
    offset = np.array([10, 20, 0])
    bounded_map["map_points"] = bounded_map["map_points"] @ rotation.T + offset
    bounded_map["robot_origin"] = offset
    result = SpatialMemory._constrain_object_to_map(
        {"position": [10, 27, 1], "point_count": 0}, bounded_map
    )
    np.testing.assert_allclose(result["position"], [10, 21.7, 1])
    np.testing.assert_allclose(
        np.linalg.norm(
            np.array(result["position"])[:2] - np.array(result["boundary_position"])[:2]
        ),
        0.3,
    )


@pytest.mark.parametrize(
    "points",
    [
        np.empty((0, 3)),
        np.array([[0, 0, 1], [1, 0, 1], [2, 0, 1]]),
        np.full((3, 3), np.nan),
    ],
)
def test_unusable_map_does_not_persist_guessed_position(bounded_map, points):
    bounded_map["map_points"] = points
    assert (
        SpatialMemory._constrain_object_to_map(
            {"position": [7, 0, 1], "point_count": 0}, bounded_map
        )
        is None
    )


def test_sparse_boundary_does_not_snap_to_unrelated_wall(bounded_map):
    bounded_map["map_points"] = bounded_map["map_points"][:4]
    assert (
        SpatialMemory._constrain_object_to_map(
            {"position": [7, 0, 1], "point_count": 0}, bounded_map
        )
        is None
    )


def test_manual_and_automatic_object_tag_share_map_limit(memory, geometry, bounded_map, mocker):
    geometry.update(bounded_map)
    geometry["camera_origin"] = np.array([0, 0, 1])
    geometry["camera_rotation"] = np.array([[0, 0, 1], [0, 1, 0], [-1, 0, 0]])
    geometry["world_points"] = np.array([[7, 0, 1]])
    mocker.patch.object(memory, "get_robot_locations", return_value=[])
    add = mocker.patch.object(memory, "add_named_location", return_value=True)
    image = Image.from_numpy(np.zeros((100, 100, 3), dtype=np.uint8))

    automatic = memory._estimate_object_position([40, 40, 60, 60], context=geometry)
    manual = json.loads(
        memory.tag_object_from_observation("chair", [40, 40, 60, 60], image, geometry)
    )

    assert automatic == [1.7, 0, 1]
    assert manual["position"] == automatic
    assert add.call_args.args[1] == automatic


def test_locate_in_observation_returns_the_lidar_position_without_storing_a_tag(
    memory, geometry, mocker
):
    mocker.patch.object(memory, "get_robot_locations", return_value=[])
    add = mocker.patch.object(memory, "add_named_location")
    tag = mocker.patch.object(memory, "_tag_object_location")
    image = Image.from_numpy(np.zeros((100, 100, 3), dtype=np.uint8))

    result = memory.locate_in_observation("person white t-shirt", [40, 40, 60, 60], image, geometry)

    assert result == {"position": [14, 20, 1], "method": "pointcloud_bbox", "point_count": 3}
    add.assert_not_called()
    tag.assert_not_called()


def test_locate_in_observation_without_lidar_hit_is_none_not_a_default_distance(memory, geometry):
    geometry["world_points"] = np.empty((0, 3))
    image = Image.from_numpy(np.zeros((100, 100, 3), dtype=np.uint8))

    assert memory.locate_in_observation("person", [40, 40, 60, 60], image, geometry) is None


def test_update_robot_location_moves_the_tag_and_restamps_it(memory, tag_store):
    tag = RobotLocation(
        "person: white t-shirt",
        (1, 2, 0.9),
        (0, 0, 0),
        location_id="person-1",
        timestamp=100.0,
        metadata={"kind": "person", "description": "Person seen wearing: white t-shirt"},
    )
    tag_store.location_collection.get.return_value = {"metadatas": [tag.to_vector_metadata()]}

    assert memory.update_robot_location("person-1", [5.0, 6.0, 0.9])

    (call,) = tag_store.location_collection.update.call_args_list
    assert call.kwargs["ids"] == ["person-1"]
    (metadata,) = call.kwargs["metadatas"]
    assert (metadata["pos_x"], metadata["pos_y"], metadata["pos_z"]) == (5.0, 6.0, 0.9)
    assert metadata["timestamp"] > 100.0
    # Everything else about the tag is kept.
    assert metadata["kind"] == "person"
    assert metadata["location_name"] == "person: white t-shirt"


def test_update_robot_location_of_an_unknown_tag_is_false(memory, tag_store):
    tag_store.location_collection.get.return_value = {"metadatas": []}

    assert not memory.update_robot_location("missing", [1.0, 2.0, 0.0])
    tag_store.location_collection.update.assert_not_called()


@pytest.mark.parametrize("position", [[1.0, 2.0], [1.0, float("nan"), 0.0]])
def test_update_robot_location_rejects_bad_positions(memory, position):
    with pytest.raises(ValueError, match="three finite numbers"):
        memory.update_robot_location("person-1", position)


def test_update_robot_location_is_refused_during_a_pgo_session(memory, tag_store, mocker):
    mocker.patch.object(memory, "_ensure_pgo_ready")
    memory._pgo_session = "session"

    assert not memory.update_robot_location("person-1", [1.0, 2.0, 0.0])
    tag_store.location_collection.update.assert_not_called()
