# Copyright 2025-2026 Dimensional Inc.
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

"""Fast unit tests for the scene-map loading knob (SpatialConfig.scene_map_dir).

The heavy parts (ChromaDB client, image-embedding model, visual memory, vector
DB) are monkeypatched so we can assert the path-derivation logic without
loading CLIP / torch.
"""

import json
import os
import queue
import tempfile

import chromadb
import cv2
import numpy as np
import open3d as o3d
import pytest

from dimos.msgs.geometry_msgs.Transform import Transform
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.sensor_msgs.CameraInfo import CameraInfo
from dimos.msgs.sensor_msgs.PointCloud2 import PointCloud2
import dimos.perception.experimental.spatial_perception as sp
from dimos.perception.experimental.spatial_perception import SpatialConfig, SpatialMemory
from dimos.perception.experimental.vlm_caption_provider import VlmCaptionProvider

_created: list[SpatialMemory] = []


@pytest.fixture(autouse=True)
def _stop_created_modules() -> None:
    yield
    for m in _created:
        try:
            m.stop()
        except Exception:  # pragma: no cover
            pass
    _created.clear()


@pytest.fixture(autouse=True)
def _patch_heavy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the heavy collaborators so constructing SpatialMemory is cheap."""
    monkeypatch.setattr(chromadb, "PersistentClient", lambda *a, **k: object())

    class FakeEmbeddingProvider:
        def __init__(self, *a, **k) -> None: ...

        def start(self) -> None: ...

        def stop(self) -> None: ...

        def get_embedding(self, *a, **k) -> object:
            import numpy as np

            return np.zeros(3)

        def get_text_embedding(self, *a, **k) -> object:
            import numpy as np

            return np.zeros(3)

    monkeypatch.setattr(sp, "ImageEmbeddingProvider", FakeEmbeddingProvider)

    class FakeVM:
        def __init__(self, *a, **k) -> None: ...  # accept any init

        def clear(self, *a, **k) -> None: ...  # called by stop()/save()

        def save(self, *a, **k) -> None: ...

    monkeypatch.setattr(sp, "VisualMemory", FakeVM)
    monkeypatch.setattr(sp, "SpatialVectorDB", lambda *a, **k: object())


def test_scene_map_dir_is_a_config_field() -> None:
    assert "scene_map_dir" in SpatialConfig.model_fields
    assert SpatialConfig().scene_map_dir is None


def test_scene_map_dir_derives_paths() -> None:
    d = tempfile.mkdtemp()
    m = SpatialMemory(scene_map_dir=d)
    _created.append(m)
    try:
        assert m.db_path == os.path.join(d, "chromadb_data")
        assert m.visual_memory_path == os.path.join(d, "visual_memory.pkl")
        # loading an existing map must not rebuild it
        assert m.config.new_memory is False
    finally:
        m.stop()


def test_no_scene_map_dir_uses_config_defaults() -> None:
    # config-level: no scene_map_dir leaves the defaults untouched
    cfg = SpatialConfig()
    assert cfg.scene_map_dir is None
    assert cfg.new_memory is True
    assert cfg.db_path.endswith("chromadb_data")
    assert cfg.visual_memory_path.endswith("visual_memory.pkl")


def test_bbox_projection_uses_world_pose_and_rejects_outside_points():
    rotation = np.array([[0, 0, 1], [0, 1, 0], [-1, 0, 0]])
    origin = np.array([10, 20, 1])
    camera_points = np.array([[0, 0, 4], [0.1, 0, 4], [-0.1, 0, 4], [2, 0, 1]])
    context = {
        "image_size": (100, 100),
        "intrinsics": [[100, 0, 50], [0, 100, 50], [0, 0, 1]],
        "camera_origin": origin,
        "camera_rotation": rotation,
        "world_points": camera_points @ rotation.T + origin,
    }
    result = SpatialMemory._project_bbox_position([40, 40, 60, 60], context, 10, 2)
    assert result == {"position": [14.0, 20.0, 1.0], "method": "pointcloud_bbox", "point_count": 3}


def test_bbox_projection_fallback_and_invalid_box():
    context = {
        "image_size": (100, 100),
        "intrinsics": [[100, 0, 50], [0, 100, 50], [0, 0, 1]],
        "camera_origin": [1, 2, 3],
        "camera_rotation": np.eye(3),
        "world_points": [[0, 0, -1], [float("nan"), 0, 1]],
    }
    result = SpatialMemory._project_bbox_position([40, 40, 60, 60], context, 10, 2)
    assert result == {"position": [1.0, 2.0, 5.0], "method": "default_distance", "point_count": 0}
    assert SpatialMemory._project_bbox_position([60, 40, 40, 60], context, 10, 2) is None


@pytest.fixture
def memory(tmp_path, mocker):
    module = SpatialMemory(db_path=str(tmp_path / "db"), output_dir=str(tmp_path))
    _created.append(module)
    mocker.patch.object(module, "vector_db", mocker.Mock())
    stored = []
    module.vector_db.tag_location.side_effect = stored.append
    module.vector_db.get_robot_locations.return_value = stored
    mocker.patch.object(module, "_tf", mocker.Mock())
    return module


@pytest.fixture
def captured_context():
    return {
        "image_size": (100, 100),
        "intrinsics": [[100, 0, 50], [0, 100, 50], [0, 0, 1]],
        "camera_origin": [1, 2, 3],
        "camera_rotation": np.eye(3),
        "world_points": [[1, 2, 7], [1.1, 2, 7], [0.9, 2, 7]],
    }


def test_named_location_honors_supplied_target_without_live_tf(memory):
    memory._tf.get.return_value = None
    assert memory.add_named_location("chair", [1, 2, 7], [0, 0, 1]) is True
    assert memory.find_robot_location("chair").position == (1.0, 2.0, 7.0)
    assert memory.find_robot_location("chair").rotation == (0.0, 0.0, 1.0)
    memory._tf.get.assert_not_called()


def test_async_result_registers_captured_targets_and_writes_immediately(memory, captured_context, tmp_path):
    memory.config.vlm_enable_object_tagging = True
    memory.config.vlm_enable_place_tagging = True
    memory._vlm_report_path = tmp_path / "tags.jsonl"
    memory._tf.get.return_value = Transform(translation=Vector3(100, 200, 300))
    memory._handle_vlm_result({
        "frame_id": "old_frame", "caption": "Kitchen entrance with a chair",
        "place": "kitchen", "place_bbox": [40, 40, 60, 60],
        "items": [{"name": "chair", "bbox": [40, 40, 60, 60]}],
        "position": [1, 2, 3], "rotation": [0, 0, 0], "timestamp": 10,
        "projection_context": captured_context,
    })
    assert memory.find_robot_location("chair").position == (1.0, 2.0, 7.0)
    assert memory.find_robot_location("kitchen").position == (1.0, 2.0, 3.0)
    report = json.loads(memory._vlm_report_path.read_text())
    assert report["objects"] == {"chair": [1.0, 2.0, 7.0]}
    assert report["object_estimates"]["chair"]["method"] == "pointcloud_bbox"
    assert report["place_position"] == [1.0, 2.0, 3.0]
    assert report["place_estimate"] == {"method": "robot_observation_pose"}


def test_place_without_box_uses_observation_pose(memory):
    memory.config.vlm_enable_place_tagging = True
    memory._handle_vlm_result({
        "frame_id": "old_frame", "caption": "A kitchen", "place": "kitchen",
        "position": [1, 2, 3], "rotation": [0, 0, 0],
    })
    assert memory.find_robot_location("kitchen").position == (1.0, 2.0, 3.0)
    assert memory._vlm_report[-1]["place_estimate"] == {"method": "robot_observation_pose"}


def test_capture_transforms_sensor_cloud_and_rejects_stale_cloud(memory):
    memory._latest_camera_info = CameraInfo(
        width=100, height=100, frame_id="camera_optical",
        K=[100, 0, 50, 0, 100, 50, 0, 0, 1],
    )
    cloud = o3d.geometry.PointCloud()
    cloud.points = o3d.utility.Vector3dVector([[0, 0, 4]])
    memory._latest_pointcloud = PointCloud2(cloud, frame_id="lidar", ts=10)
    memory._tf.get.side_effect = [
        Transform(translation=Vector3(1, 2, 3)),
        Transform(translation=Vector3(1, 2, 3)),
    ]
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    context = memory._capture_projection_context(frame, 10)
    np.testing.assert_allclose(context["world_points"], [[1, 2, 7]])
    memory._tf.get.assert_any_call(
        "world", "lidar", time_point=10, time_tolerance=1.0, warn=False,
    )
    memory._tf.get.side_effect = None
    memory._tf.get.return_value = Transform(translation=Vector3(50, 50, 50))
    stale_context = memory._capture_projection_context(frame, 20)
    assert stale_context["world_points"].shape == (0, 3)
    np.testing.assert_allclose(context["camera_origin"], [1, 2, 3])


def test_worker_preserves_geometry_and_place_box(mocker, captured_context):
    parsed = VlmCaptionProvider._parse_json_response(
        '{"caption":"Kitchen entrance", "place":"kitchen", "place_bbox":[40,40,60,60]}'
    )
    mocker.patch.object(VlmCaptionProvider, "analyze", return_value=parsed)
    tasks = queue.Queue()
    results = queue.Queue()
    success, encoded = cv2.imencode(".jpg", np.zeros((100, 100, 3), dtype=np.uint8))
    assert success
    tasks.put({"frame_id": "old_frame", "frame_array": encoded, "projection_context": captured_context})
    tasks.put(None)
    sp._vlm_worker(
        {"base_url": "https://example.invalid", "model": "test", "prompt": "test",
         "max_tokens": 10, "timeout": 1, "api_key": "dummy"}, tasks, results,
    )
    result = results.get_nowait()
    assert result["place_bbox"] == [40, 40, 60, 60]
    assert result["projection_context"] is captured_context
    assert result["frame_bgr"].shape == (100, 100, 3)


def test_sparse_depth_cluster_is_finite(captured_context):
    captured_context["world_points"] = [[1, 2, 4], [1, 2, 12]]
    estimate = SpatialMemory._project_bbox_position([40, 40, 60, 60], captured_context, 10, 2)
    assert estimate == {"position": [1.0, 2.0, 4.0], "method": "pointcloud_bbox", "point_count": 1}


def test_segmentation_mask_filters_box_background(captured_context):
    captured_context["world_points"] = [[0.4, 2, 6], [1, 2, 7]]
    mask = np.zeros((100, 100), dtype=bool)
    mask[45:55, 45:55] = True
    captured_context["mask"] = mask
    estimate = SpatialMemory._project_bbox_position([20, 20, 80, 80], captured_context, 10, 2)
    assert estimate == {"position": [1.0, 2.0, 7.0], "method": "pointcloud_bbox", "point_count": 1}


def test_result_is_written_even_without_a_new_frame(memory, captured_context, tmp_path):
    memory._vlm_report_path = tmp_path / "stationary_tags.jsonl"
    memory._vlm_result_queue = queue.Queue()
    memory._vlm_result_queue.put({
        "frame_id": "old_frame", "caption": "A chair", "place": None,
        "position": [1, 2, 3], "rotation": [0, 0, 0],
        "projection_context": captured_context,
        "items": [{"name": "chair", "bbox": [40, 40, 60, 60]}],
    })
    memory._process_frame()
    report = json.loads(memory._vlm_report_path.read_text())
    assert report["detections"] == [{"name": "chair", "bbox": [40, 40, 60, 60]}]
