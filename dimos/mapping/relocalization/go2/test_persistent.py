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

from pathlib import Path
from threading import Event

from dimos_lcm.std_msgs import Bool
import numpy as np
import pytest
from pytest_mock import MockerFixture
from scipy.spatial.transform import Rotation

from dimos.core.coordination.blueprint_config.parser import BlueprintConfigParser
from dimos.core.coordination.module_coordinator import (
    _resolve_single_ref,
    _verify_no_name_conflicts,
)
from dimos.core.module import Module
from dimos.mapping.relocalization.go2 import persistent
from dimos.mapping.relocalization.go2.persistent import PersistentGo2Map, PersistentGo2Planner
from dimos.mapping.voxels.module import VoxelGridMapper
from dimos.msgs.geometry_msgs.PointStamped import PointStamped
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Transform import Transform
from dimos.msgs.geometry_msgs.Twist import Twist
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.nav_msgs.OccupancyGrid import OccupancyGrid
from dimos.msgs.sensor_msgs.PointCloud2 import PointCloud2
from dimos.msgs.tf2_msgs.TFMessage import TFMessage
from dimos.navigation.go2.loop_closure.pgo import Keyframe, PoseGraph
from dimos.navigation.go2.replanning_a_star.module import ReplanningAStarPlanner
from dimos.robot.unitree.go2.blueprints.agentic.unitree_go2_agentic import unitree_go2_agentic
from dimos.robot.unitree.go2.blueprints.agentic.unitree_go2_agentic_persistent import (
    unitree_go2_agentic_persistent,
)
from dimos.robot.unitree.go2.connection import GO2Connection
from dimos.types.robot_location import RobotLocation
from dimos.visualization.rerun.bridge import RerunBridgeModule


@pytest.fixture
def session(tmp_path: Path, mocker: MockerFixture):
    built = []
    mocker.patch.object(Module, "start")
    mocker.patch.object(persistent, "LidarRelocalizer")

    def build(create_new=False, map_file=None, **config):
        module = PersistentGo2Map(
            map_file=str(map_file or tmp_path / "office.pc2.lcm"),
            create_new=create_new,
            save_interval=0,
            emit_every=1,
            min_local_points=1,
            **config,
        )
        built.append(module)
        if config.get("pgo_enabled"):
            mocker.patch.object(module, "_pgo_memory", mocker.Mock())
            mocker.patch.object(module, "_pgo_navigation", mocker.Mock())
        for port in module.inputs.values():
            mocker.patch.object(port, "subscribe", return_value=lambda: None)
        for port in module.outputs.values():
            mocker.patch.object(port, "publish", side_effect=lambda message: message.lcm_encode())
        module.start()
        return module

    yield build
    for module in built:
        module.stop()
        module.dispose()


def cloud(points):
    return PointCloud2.from_numpy(np.asarray(points, dtype=np.float32), timestamp=12.0)


def test_stationary_gate_prevents_map_pollution_but_keeps_live_streams(session, mocker):
    clock = mocker.patch.object(persistent.time, "monotonic", return_value=0.0)
    module = session(create_new=True, auto_pause_fusion=True)
    for i in range(41):
        clock.return_value = i * 0.125
        pose = PoseStamped(frame_id="world", position=[i * 0.001, 0, 0], ts=i * 0.125)
        module._on_odom(pose)
        module._on_tf(TFMessage(Transform.from_pose("base_link", pose)))
        module._on_lidar(cloud([[i, 0, 0]]))
        if i == 20:
            before = module._current_map().lcm_encode()
            accepted = module._frames

    assert module._current_map().lcm_encode() == before
    assert module._frames == accepted
    assert module.fusion_status()["motion_state"] == "stationary"
    assert module.lidar.publish.call_count == 41
    assert module.odom.publish.call_count == 41
    assert module.tf.publish.call_count == 41
    module.save_map()
    assert Path(module.config.map_file).read_bytes() == before


@pytest.mark.parametrize("pgo_enabled", [False, True])
def test_manual_pause_blocks_fusion_not_lidar_and_resume_restores_legacy_behavior(
    session, mocker, pgo_enabled
):
    pgo = mocker.patch.object(persistent, "PGOMap").return_value
    pgo.add.return_value = False
    pgo.graph.return_value = PoseGraph()
    pgo.global_map.return_value = cloud([[0, 0, 0]])
    module = session(create_new=True, pgo_enabled=pgo_enabled)
    module._on_lidar(cloud([[0, 0, 0]]))
    module.pause_fusion()
    for i in range(3):
        module._on_odom(PoseStamped(frame_id="world", position=[i, 0, 0]))
        module._on_lidar(cloud([[i + 1, 0, 0]]))
    assert module._frames == 1
    assert module.lidar.publish.call_count == 4
    assert module.fusion_status()["skipped_frames"] == 3
    if pgo_enabled:
        assert pgo.add.call_count == 1
        assert module.pgo_raw_lidar.publish.call_count == 4
    else:
        assert len(module._current_map()) == 1
    module.resume_fusion()
    module._on_lidar(cloud([[4, 0, 0]]))
    assert module._frames == 2


def test_resume_keeps_automatic_pause_and_stale_odometry_blocks_fusion(session, mocker):
    clock = mocker.patch.object(persistent.time, "monotonic", return_value=0.0)
    module = session(create_new=True, auto_pause_fusion=True)
    module._on_lidar(cloud([[0, 0, 0]]))
    assert module._frames == 0
    for i in range(21):
        clock.return_value = i * 0.125
        module._on_odom(PoseStamped(frame_id="world"))
        module._on_lidar(cloud([[0, 0, 0]]))
    module.pause_fusion()
    module.resume_fusion()
    assert module.fusion_status()["fusion_enabled"] is False
    assert module.fusion_status()["manual_paused"] is False
    clock.return_value = 5.0
    module._on_lidar(cloud([[100, 0, 0]]))
    assert module.fusion_status()["motion_state"] == "unknown"
    assert "stale" in module.fusion_status()["reason"]
    assert len(module._current_map()) == 1


def test_paused_pgo_still_corrects_live_cloud_without_inserting_frame(session, mocker):
    pgo = mocker.patch.object(persistent, "PGOMap").return_value
    module = session(create_new=True, pgo_enabled=True)
    module._pgo_graph = fixed_graph()
    module.pause_fusion()
    module._on_lidar(cloud([[2, 0, 0]]))
    pgo.add.assert_not_called()
    assert module._frames == 0
    np.testing.assert_allclose(
        module.pgo_raw_lidar.publish.call_args.args[0].as_numpy()[0], [[2, 0, 0]]
    )
    np.testing.assert_allclose(module.lidar.publish.call_args.args[0].as_numpy()[0], [[2.5, 0, 0]])


def test_restore_startup_capture_ignores_fusion_gate_then_obeys_manual_pause(
    session, tmp_path, mocker
):
    save_premap(tmp_path / "office.pc2.lcm")
    module = session(auto_pause_fusion=True, manual_capture=True)
    scans = mocker.patch.object(module._scans, "on_next")
    module.pause_fusion()
    module._on_lidar(cloud([[0, 0, 0], [1, 0, 0]]))
    assert module._capture_frames == 1
    module.finish_startup_capture()
    assert len(scans.call_args.args[0]) == 2
    module._candidate = Transform(frame_id="world", child_frame_id="world")
    module.confirm_alignment()
    module._on_lidar(cloud([[100, 0, 0]]))
    assert module._frames == 0
    assert len(module._current_map()) == 3
    assert module.fusion_status()["manual_paused"] is True
    assert module.navigation_ready() is True


def save_premap(path):
    prior = cloud([[0, 0, 0], [4, 2, 0], [4, 2, 1]])
    path.write_bytes(prior.lcm_encode())
    return prior


def fixed_graph():
    return PoseGraph(
        keyframes=(
            Keyframe(
                10, Transform(translation=Vector3(1, 0, 0)), Transform(translation=Vector3(1, 0, 0))
            ),
            Keyframe(
                12,
                Transform(translation=Vector3(2, 0, 0)),
                Transform(translation=Vector3(2.5, 0, 0)),
            ),
        )
    )


def test_pgo_corrects_live_pose_cloud_tf_and_saves_without_moving_old_map(
    session, tmp_path, mocker
):
    path = tmp_path / "office.pc2.lcm"
    prior = save_premap(path)
    pgo = mocker.patch.object(persistent, "PGOMap").return_value
    pgo.add.return_value = True
    pgo.graph.return_value = fixed_graph()
    pgo.global_map.return_value = cloud([[2.5, 0, 0]])
    module = session(pgo_enabled=True)
    module._candidate = Transform(frame_id="world", child_frame_id="world")
    module.confirm_alignment()
    pose = PoseStamped(frame_id="world", position=[2, 0, 0], ts=12.0)
    module._on_odom(pose)
    module._on_tf(
        TFMessage(
            Transform(
                translation=Vector3(2, 0, 0),
                frame_id="world",
                child_frame_id="base_link",
                ts=12.0,
            )
        )
    )

    module._on_lidar(cloud([[2, 0, 0]]))

    np.testing.assert_allclose(module.lidar.publish.call_args.args[0].points_f32(), [[2.5, 0, 0]])
    assert module.odom.publish.call_args.args[0].position.x == pytest.approx(2.5)
    assert module.tf.publish.call_args.args[0].transforms[0].translation.x == pytest.approx(2.5)
    assert module.pgo_raw_tf.publish.call_args.args[0].transforms[0].translation.x == 2
    np.testing.assert_allclose(
        module.pgo_raw_lidar.publish.call_args.args[0].points_f32(), [[2, 0, 0]]
    )
    saved = PointCloud2.lcm_decode(path.read_bytes()).points_f32()
    np.testing.assert_allclose(saved[: len(prior)], prior.points_f32())
    np.testing.assert_allclose(saved[-1], [2.5, 0, 0])
    module._pgo_memory.update_pgo_graph.assert_called_with(
        pgo.graph.return_value, module._pgo_session, mocker.ANY
    )
    module._pgo_navigation.pause_for_pgo.assert_called_once()
    module._pgo_navigation.resume_after_pgo.assert_called_once()
    assert module._pgo_navigation.resume_after_pgo.call_args.args[2].position.x == pytest.approx(
        2.5
    )
    module.pgo_stop.publish.assert_not_called()
    pgo.flush.assert_called_once()


def test_pgo_memory_sync_failure_blocks_motion_navigation_and_map_overwrite(
    session, tmp_path, mocker
):
    path = tmp_path / "office.pc2.lcm"
    save_premap(path)
    saved = path.read_bytes()
    pgo = mocker.patch.object(persistent, "PGOMap").return_value
    pgo.add.return_value = True
    pgo.graph.return_value = fixed_graph()
    pgo.global_map.return_value = cloud([[2.5, 0, 0]])
    module = session(pgo_enabled=True)
    module._candidate = Transform(frame_id="world", child_frame_id="world")
    module.confirm_alignment()
    module._pgo_memory.update_pgo_graph.side_effect = RuntimeError("Tag database unavailable")

    with pytest.raises(RuntimeError, match="Tag database"):
        module._on_lidar(cloud([[2, 0, 0]]))
    assert not module.navigation_ready()
    module._on_cmd_vel(Twist(linear=Vector3(0.3, 0, 0)))
    assert module.cmd_vel.publish.call_args.args[0].linear.x == 0
    with pytest.raises(RuntimeError, match="PGO synchronization"):
        module.save_map()
    assert path.read_bytes() == saved


def test_pgo_checkpoint_failure_blocks_navigation_and_preserves_saved_map(
    session, tmp_path, mocker
):
    path = tmp_path / "office.pc2.lcm"
    save_premap(path)
    saved = path.read_bytes()
    pgo = mocker.patch.object(persistent, "PGOMap").return_value
    pgo.add.return_value = True
    pgo.graph.return_value = fixed_graph()
    pgo.global_map.return_value = cloud([[2.5, 0, 0]])
    module = session(pgo_enabled=True)
    module._candidate = Transform(frame_id="world", child_frame_id="world")
    module.confirm_alignment()
    mocker.patch.object(module, "save_map", side_effect=OSError("disk full"))

    with pytest.raises(OSError, match="disk full"):
        module._on_lidar(cloud([[2, 0, 0]]))

    assert not module.navigation_ready()
    assert module.cmd_vel.publish.call_args.args[0].linear.x == 0
    assert path.read_bytes() == saved


def test_new_map_save_restore_requires_confirmation(session, tmp_path):
    first = session(create_new=True)
    first._on_lidar(cloud([[0, 0, 0], [4, 2, 0], [4, 2, 1]]))
    first.save_map()
    saved = (tmp_path / "office.pc2.lcm").read_bytes()
    restored = session()

    assert first.navigation_ready()
    assert not restored.navigation_ready()
    restored._on_lidar(cloud([[1, 0, 0]]))
    restored._on_odom(PoseStamped(frame_id="world", position=[1, 0, 0]))
    restored._on_tf(TFMessage(Transform(frame_id="world", child_frame_id="base_link")))
    restored.global_map.publish.assert_not_called()
    restored.lidar.publish.assert_not_called()
    restored.odom.publish.assert_not_called()
    restored.tf.publish.assert_not_called()
    restored._autosave()
    assert (tmp_path / "office.pc2.lcm").read_bytes() == saved
    with pytest.raises(RuntimeError, match="Cannot save"):
        restored.save_map()
    with pytest.raises(RuntimeError, match="No alignment candidate"):
        restored.confirm_alignment()


def test_confirmation_transforms_cloud_pose_and_tf_in_the_same_direction(session, tmp_path):
    save_premap(tmp_path / "office.pc2.lcm")
    module = session()
    # The candidate places the old map in the new session; live data needs its inverse.
    candidate_matrix = np.eye(4)
    candidate_matrix[:3, :3] = Rotation.from_euler("z", 90, degrees=True).as_matrix()
    candidate_matrix[:3, 3] = [10, 5, 0]
    candidate = Transform.from_matrix(candidate_matrix, frame_id="world", child_frame_id="map")
    module._relocalizer.relocalize.return_value = candidate
    module._match(cloud([[10, 6, 0]]))
    assert not module.navigation_ready()
    assert not hasattr(module.confirm_alignment, "__skill__")
    module._on_odom(PoseStamped(frame_id="world", ts=12.0, position=[10, 6, 0]))
    root = Transform(
        frame_id="world", child_frame_id="base_link", translation=Vector3(10, 6, 0), ts=12.0
    )
    camera = Transform(frame_id="base_link", child_frame_id="camera_link", ts=12.0)
    module._on_tf(TFMessage(root, camera))

    module.confirm_alignment()
    module._on_lidar(cloud([[10, 6, 0]]))

    assert module.navigation_ready()
    placed_cloud = module.lidar.publish.call_args.args[0]
    placed_pose = module.odom.publish.call_args.args[0]
    placed_tf = module.tf.publish.call_args.args[0].transforms
    np.testing.assert_allclose(placed_cloud.as_numpy()[0], [[1, 0, 0]], atol=1e-6)
    np.testing.assert_allclose(placed_pose.position.to_numpy(), [1, 0, 0], atol=1e-6)
    np.testing.assert_allclose(placed_tf[0].translation.to_numpy(), [1, 0, 0], atol=1e-6)
    assert placed_pose.ts == placed_tf[0].ts == 12
    assert placed_cloud.frame_id == placed_pose.frame_id == placed_tf[0].frame_id == "world"
    assert placed_tf[1] is camera
    cleared = module.alignment_scan.publish.call_args.args[0]
    assert cleared.ts is not None
    assert len(PointCloud2.lcm_decode(cleared.lcm_encode())) == 0


def test_rejected_candidate_never_enables_navigation(session, tmp_path):
    save_premap(tmp_path / "office.pc2.lcm")
    module = session()
    module._relocalizer.relocalize.return_value = Transform.from_matrix(
        np.eye(4), frame_id="world", child_frame_id="map"
    )
    module._match(cloud([[0, 0, 0]]))
    module.reject_alignment()
    cleared = module.alignment_preview.publish.call_args.args[0]
    assert cleared.ts is not None
    assert len(PointCloud2.lcm_decode(cleared.lcm_encode())) == 0
    assert not module.navigation_ready()
    with pytest.raises(RuntimeError, match="No alignment candidate"):
        module.confirm_alignment()
    module._match(cloud([[0, 0, 0]]))
    assert module._candidate is not None
    module.confirm_alignment()
    with pytest.raises(RuntimeError, match="Cannot reject"):
        module.reject_alignment()


def test_failed_preview_clear_does_not_approve_alignment(session, tmp_path, mocker):
    save_premap(tmp_path / "office.pc2.lcm")
    module = session()
    module._relocalizer.relocalize.return_value = Transform.from_matrix(
        np.eye(4), frame_id="world", child_frame_id="map"
    )
    module._match(cloud([[0, 0, 0]]))
    mocker.patch.object(module.alignment_preview, "publish", side_effect=RuntimeError("offline"))

    with pytest.raises(RuntimeError, match="offline"):
        module.confirm_alignment()
    assert not module.navigation_ready()
    assert module._candidate is not None
    with pytest.raises(RuntimeError, match="offline"):
        module.reject_alignment()
    assert module._candidate is not None


def test_confirmed_map_preserves_unobserved_regions_and_updates_seen_columns(session, tmp_path):
    save_premap(tmp_path / "office.pc2.lcm")
    module = session()
    module._relocalizer.relocalize.return_value = Transform.from_matrix(
        np.eye(4), frame_id="world", child_frame_id="map"
    )
    module._match(cloud([[0, 0, 0]]))
    module.confirm_alignment()
    module._on_lidar(cloud([[4, 2, 0], [4, 2, 0.5], [7, 0, 0]]))
    module.save_map()
    saved = PointCloud2.lcm_decode((tmp_path / "office.pc2.lcm").read_bytes())
    points = saved.as_numpy()[0]
    assert np.any(np.linalg.norm(points - [0, 0, 0], axis=1) < 0.1)
    assert np.any(np.linalg.norm(points - [7, 0, 0], axis=1) < 0.1)
    assert np.any(np.linalg.norm(points - [4, 2, 0.5], axis=1) < 0.1)
    assert not np.any(np.linalg.norm(points - [4, 2, 1], axis=1) < 0.1)
    assert saved.frame_id == "world"


@pytest.mark.parametrize("pgo_enabled", [False, True])
def test_restore_final_save_stop_reload_keeps_old_and_new_points(
    session, tmp_path, mocker, pgo_enabled
):
    path = tmp_path / "office.pc2.lcm"
    save_premap(path)
    tags = tmp_path / "tags.json"
    tags.write_text('{"office": [4, 2, 0]}')
    if pgo_enabled:
        pgo = mocker.patch.object(persistent, "PGOMap").return_value
        pgo.add.return_value = False
        pgo.graph.return_value = PoseGraph()
        pgo.global_map.return_value = cloud([[7, 0, 0]])
    module = session(pgo_enabled=pgo_enabled)
    module._relocalizer.relocalize.return_value = Transform.from_matrix(
        np.eye(4), frame_id="world", child_frame_id="map"
    )
    module._match(cloud([[0, 0, 0]]))
    module.confirm_alignment()
    module._on_lidar(cloud([[7, 0, 0]]))

    result = module.prepare_map_shutdown()
    saved = path.read_bytes()
    spy = mocker.spy(module, "save_map")
    assert module.prepare_map_shutdown() == result
    module._on_lidar(cloud([[8, 0, 0]]))
    module._autosave()
    module.stop()
    reloaded = session()
    points = reloaded._premap.points_f32()

    assert result["state"] == "saved"
    assert result["path"] == str(path)
    assert result["accepted_frames"] == 1
    spy.assert_not_called()
    assert path.read_bytes() == saved
    assert tags.read_text() == '{"office": [4, 2, 0]}'
    assert any(np.allclose(point, [0, 0, 0], atol=0.1) for point in points)
    assert any(np.allclose(point, [7, 0, 0], atol=0.1) for point in points)
    assert not any(np.allclose(point, [8, 0, 0], atol=0.1) for point in points)
    assert not module.navigation_ready()


def test_final_save_failure_does_not_seal_ingestion_or_replace_old_map(session, tmp_path, mocker):
    path = tmp_path / "office.pc2.lcm"
    save_premap(path)
    original = path.read_bytes()
    module = session()
    module._relocalizer.relocalize.return_value = Transform.from_matrix(
        np.eye(4), frame_id="world", child_frame_id="map"
    )
    module._match(cloud([[0, 0, 0]]))
    module.confirm_alignment()
    module._on_lidar(cloud([[7, 0, 0]]))
    replacement = mocker.patch.object(Path, "replace", side_effect=OSError("disk full"))
    with pytest.raises(OSError, match="disk full"):
        module.prepare_map_shutdown()
    mocker.stop(replacement)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]
    assert module.navigation_ready()
    module._on_lidar(cloud([[8, 0, 0]]))
    assert module._frames == 2


def test_no_accepted_scans_explains_fusion_pause_and_explicit_stop_preserves_file(
    session, tmp_path
):
    path = tmp_path / "office.pc2.lcm"
    save_premap(path)
    original = path.read_bytes()
    module = session()
    with pytest.raises(RuntimeError, match="alignment has not been approved"):
        module.prepare_map_shutdown()
    module._relocalizer.relocalize.return_value = Transform.from_matrix(
        np.eye(4), frame_id="world", child_frame_id="map"
    )
    module._match(cloud([[0, 0, 0]]))
    module.confirm_alignment()
    module.pause_fusion()
    module._on_lidar(cloud([[7, 0, 0]]))
    with pytest.raises(RuntimeError, match="no accepted scans.*Skipped 1"):
        module.prepare_map_shutdown()
    assert module.prepare_map_shutdown(save=False)["state"] == "not_saved"
    module.stop()
    assert path.read_bytes() == original


def test_explicit_stop_without_saving_does_not_autosave_new_frames(session, tmp_path):
    path = tmp_path / "office.pc2.lcm"
    save_premap(path)
    original = path.read_bytes()
    module = session()
    module._relocalizer.relocalize.return_value = Transform.from_matrix(
        np.eye(4), frame_id="world", child_frame_id="map"
    )
    module._match(cloud([[0, 0, 0]]))
    module.confirm_alignment()
    module._on_lidar(cloud([[7, 0, 0]]))
    assert module.prepare_map_shutdown(save=False)["accepted_frames"] == 1
    module._autosave()
    module.stop()
    assert path.read_bytes() == original


def test_candidate_details_heading_and_stale_confirmation(session, tmp_path):
    save_premap(tmp_path / "office.pc2.lcm")
    module = session()
    matrix = np.eye(4)
    matrix[:3, :3] = Rotation.from_euler("z", 90, degrees=True).as_matrix()
    matrix[:3, 3] = [10, 5, 0]
    module._relocalizer.relocalize.return_value = Transform.from_matrix(
        matrix, frame_id="world", child_frame_id="map"
    )
    module._relocalizer.last_fitness = 0.85
    module._relocalizer.last_rmse = 0.12
    module._on_odom(PoseStamped(frame_id="world", ts=12, position=[10, 6, 0]))
    module._match(cloud([[10, 6, 0]]))
    details = module.alignment_details()
    assert details["phase"] == "candidate"
    assert details["metrics"] == {"fitness": 0.85, "rmse_m": 0.12}
    np.testing.assert_allclose(details["robot_in_saved_map"]["position_m"], [1, 0, 0], atol=1e-6)
    assert details["robot_in_saved_map"]["yaw_deg"] == pytest.approx(-90)
    heading = module.alignment_heading.publish.call_args.args[0].points_f32()
    np.testing.assert_allclose(heading[0], [10, 6, 0])
    np.testing.assert_allclose(heading[29], [11, 6, 0])
    preview = module.alignment_preview.publish.call_args.args[0]
    scan = module.alignment_scan.publish.call_args.args[0]
    assert not np.allclose(preview.as_numpy()[1][0], scan.as_numpy()[1][0])
    module.reject_alignment_candidate(details["candidate_id"])
    module._match(cloud([[10, 6, 0]]))
    with pytest.raises(RuntimeError, match="candidate changed"):
        module.confirm_alignment_candidate(details["candidate_id"])
    assert not module.navigation_ready()
    module.confirm_alignment_candidate(module.alignment_details()["candidate_id"])
    assert module.alignment_details()["phase"] == "ready"
    assert module.alignment_details()["candidate_id"] is None
    assert len(module.alignment_heading.publish.call_args.args[0]) == 0


def test_alignment_failure_reason_is_visible_and_navigation_stays_blocked(session, tmp_path):
    save_premap(tmp_path / "office.pc2.lcm")
    module = session()
    module._relocalizer.relocalize.side_effect = RuntimeError("ICP unavailable")
    module._match(cloud([[0, 0, 0]]))
    details = module.alignment_details()
    assert details["reason"] == "ICP unavailable"
    assert details["attempts"] == 1
    assert details["candidate_id"] is None
    assert not module.navigation_ready()


def test_failed_atomic_save_preserves_existing_map(session, tmp_path, monkeypatch):
    module = session(create_new=True)
    module._on_lidar(cloud([[0, 0, 0]]))
    module.save_map()
    path = tmp_path / "office.pc2.lcm"
    original = path.read_bytes()

    def fail_replace(self, target):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail_replace)
        with pytest.raises(OSError, match="disk full"):
            module.save_map()
    assert path.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["office.pc2.lcm"]


def test_missing_map_and_create_new_never_silently_replace_data(session, tmp_path):
    with pytest.raises(FileNotFoundError, match="Saved map not found"):
        session()
    save_premap(tmp_path / "office.pc2.lcm")
    with pytest.raises(FileExistsError, match="Refusing to replace"):
        session(create_new=True)


def test_navigation_gate_refuses_before_confirmation_and_forwards_after(mocker):
    planner = PersistentGo2Planner()
    try:
        map_session = mocker.patch.object(planner, "_map_session", create=True)
        handle = mocker.patch.object(planner._planner, "handle_goal_request")
        goal = PoseStamped(frame_id="world", position=[1, 0, 0])
        map_session.navigation_ready.return_value = False
        assert planner.set_goal(goal) is False
        handle.assert_not_called()
        map_session.navigation_ready.return_value = True
        assert planner.set_goal(goal) is True
        handle.assert_called_once_with(goal)
    finally:
        planner.dispose()


@pytest.fixture
def pgo_planner(mocker):
    planner = PersistentGo2Planner()
    map_session = mocker.patch.object(planner, "_map_session", create=True)
    map_session.navigation_ready.return_value = True
    memory = mocker.patch.object(planner, "_spatial_memory", create=True)
    memory.get_robot_locations.return_value = [
        RobotLocation("fire extinguisher", (3, 4, 1), (0, 0, 0.5), location_id="ext-1"),
        RobotLocation("fire extinguisher", (99, 99, 1), (0, 0, 0), location_id="ext-2"),
    ]
    costmapper = mocker.patch.object(planner, "_costmapper", create=True)
    costmapper.calculate_navigation_costmap.return_value = OccupancyGrid(
        grid=np.zeros((20, 20), dtype=np.int8), resolution=0.5, frame_id="world", ts=12.0
    )
    for name in ("handle_goal_request", "cancel_goal", "handle_global_costmap", "handle_odom"):
        mocker.patch.object(planner._planner, name)
    for port in planner.outputs.values():
        mocker.patch.object(port, "publish")
    yield planner
    planner.dispose()


@pytest.fixture
def visual_planner(pgo_planner, mocker):
    planner = pgo_planner
    mocker.patch.object(persistent, "Thread")
    mocker.patch.object(persistent.time, "monotonic", return_value=100.0)
    matcher = mocker.patch.object(planner, "_tag_view", create=True)
    matcher.verify_tag_view.return_value = {"matched": True, "image_ts": 1000.0}
    planner.configure_visual_arrival(True)
    planner.set_nearby_tagged_goal("ext-1", PoseStamped(position=[3, 4, 0], frame_id="world"))
    planner._handle_odom(PoseStamped(position=[2, 4, 0], frame_id="world"))
    yield planner, matcher
    planner.cancel_goal()


@pytest.fixture
def speed_planner(mocker):
    planner = PersistentGo2Planner(navigation_speed_limit=0.55)
    mocker.patch.object(planner.nav_cmd_vel, "publish")
    yield planner
    planner.dispose()


def test_live_speed_caps_next_command_without_replacing_active_goal(speed_planner):
    planner = speed_planner
    goal = PoseStamped(position=[3, 0, 0])
    planner._active_goal = goal
    planner._goal_revision = 7
    command = Twist(linear=Vector3(0.55, 0, 0), angular=Vector3(0, 0, 0.3))
    planner._publish_navigation_velocity(command)
    assert planner.nav_cmd_vel.publish.call_args.args[0] is command
    assert planner.set_navigation_speed(0.1) == {"enabled": True, "speed_mps": 0.1}
    planner._publish_navigation_velocity(command)
    output = planner.nav_cmd_vel.publish.call_args.args[0]
    assert output.linear.x == pytest.approx(0.1)
    assert output.angular.z == 0.3
    assert command.linear.x == 0.55
    assert planner._active_goal is goal
    assert planner._goal_revision == 7
    planner.set_navigation_speed(0.55)
    planner._publish_navigation_velocity(command)
    assert planner.nav_cmd_vel.publish.call_args.args[0] is command


def test_speed_limit_preserves_direction_slow_commands_and_stop(speed_planner):
    planner = speed_planner
    planner.set_navigation_speed(0.25)
    planner._publish_navigation_velocity(Twist(linear=Vector3(-0.3, 0.4, 0)))
    output = planner.nav_cmd_vel.publish.call_args.args[0]
    assert output.linear.x == pytest.approx(-0.15)
    assert output.linear.y == pytest.approx(0.2)
    for command in (Twist(), Twist(linear=Vector3(0.15, 0, 0)), Twist(angular=Vector3(0, 0, 0.15))):
        planner._publish_navigation_velocity(command)
        assert planner.nav_cmd_vel.publish.call_args.args[0] is command


@pytest.mark.parametrize("speed", [True, "0.2", 0.09, 0.56, float("nan"), float("inf")])
def test_invalid_live_speed_retains_previous_limit(speed_planner, speed):
    with pytest.raises(ValueError, match="between"):
        speed_planner.set_navigation_speed(speed)
    assert speed_planner.navigation_speed_status()["speed_mps"] == 0.55


def test_original_persistent_planner_speed_is_unchanged_and_not_live_adjustable(pgo_planner):
    command = Twist(linear=Vector3(0.6, 0, 0))
    pgo_planner._publish_navigation_velocity(command)
    assert pgo_planner.nav_cmd_vel.publish.call_args.args[0] is command
    assert pgo_planner.navigation_speed_status() == {"enabled": False, "speed_mps": None}
    with pytest.raises(ValueError, match="not enabled"):
        pgo_planner.set_navigation_speed(0.25)


def test_visual_arrival_waits_for_selected_tag_image_before_announcing(visual_planner):
    planner, matcher = visual_planner
    assert planner.visual_arrival_status() == {"enabled": True, "searching": True}
    assert planner.navigation_state.publish.call_args.args[0].data == (
        "Nearby threshold reached; searching for tag image"
    )
    assert planner.is_goal_reached() is False
    planner._publish_goal_result(Bool(True))
    planner.goal_reached.publish.assert_not_called()
    planner._search_tag_view(planner._goal_revision, "ext-1", planner._search_stop)
    matcher.verify_tag_view.assert_called_once_with("ext-1")
    assert planner.navigation_state.publish.call_args.args[0].data == "Arrived: tag image matched"
    assert planner.nav_cmd_vel.publish.call_args.args[0].angular.z == 0
    assert planner.goal_reached.publish.call_args.args[0].data is True
    assert planner.is_goal_reached() is True


def test_visual_search_turns_without_translation_and_stops_on_match(visual_planner, mocker):
    planner, matcher = visual_planner
    stop = mocker.Mock(spec=Event)
    stop.is_set.return_value = False
    stop.wait.return_value = False
    mocker.patch.object(planner, "_search_stop", stop)
    matcher.verify_tag_view.side_effect = [
        {"matched": False, "image_ts": 1000.0},
        {"matched": True, "image_ts": 1001.0},
    ]
    planner._search_tag_view(planner._goal_revision, "ext-1", stop)
    commands = [call.args[0] for call in planner.nav_cmd_vel.publish.call_args_list]
    assert any(command.angular.z == 0.15 for command in commands)
    assert all(
        command.linear.x == command.linear.y == command.linear.z == 0 for command in commands
    )
    assert commands[-1].angular.z == 0
    assert planner.goal_reached.publish.call_args.args[0].data is True


@pytest.mark.parametrize("outcome", ["cancel", "pgo", "off", "replace"])
def test_slow_visual_match_cannot_restart_motion_after_cancellation(visual_planner, outcome):
    planner, matcher = visual_planner
    revision = planner._goal_revision
    stop = planner._search_stop

    def interrupt(location_id):
        if outcome == "pgo":
            planner.pause_for_pgo()
        elif outcome == "off":
            planner.configure_visual_arrival(False)
        elif outcome == "replace":
            planner.set_goal(PoseStamped(position=[9, 9, 0], frame_id="world"))
        else:
            planner.cancel_goal()
        return {"matched": True, "image_ts": 1001.0}

    matcher.verify_tag_view.side_effect = interrupt
    planner._search_tag_view(revision, "ext-1", stop)
    assert stop.is_set()
    assert all(call.args[0].angular.z == 0 for call in planner.nav_cmd_vel.publish.call_args_list)
    planner.goal_reached.publish.assert_not_called()
    assert planner.navigation_state.publish.call_args.args[0].data != "Arrived: tag image matched"


def test_visual_timeout_and_missing_image_stop_without_claiming_arrival(visual_planner, mocker):
    planner, matcher = visual_planner
    clock = mocker.patch.object(persistent.time, "monotonic", side_effect=[100.0, 121.0])
    planner._search_tag_view(planner._goal_revision, "ext-1", planner._search_stop)
    assert (
        planner.navigation_state.publish.call_args.args[0].data
        == "Visual search timed out; no matching tag view"
    )
    assert planner.goal_reached.publish.call_args.args[0].data is False
    assert planner.nav_cmd_vel.publish.call_args.args[0].angular.z == 0
    clock.side_effect = None
    clock.return_value = 100.0
    planner.set_nearby_tagged_goal("ext-1", PoseStamped(position=[3, 4, 0], frame_id="world"))
    planner._handle_odom(PoseStamped(position=[2, 4, 0], frame_id="world"))
    matcher.verify_tag_view.side_effect = ValueError("Reference image missing")
    planner._search_tag_view(planner._goal_revision, "ext-1", planner._search_stop)
    assert (
        planner.navigation_state.publish.call_args.args[0].data
        == "Visual search failed: Reference image missing"
    )
    assert planner.goal_reached.publish.call_args.args[0].data is False


def test_pgo_replans_same_tag_id_using_updated_coordinates_and_map(pgo_planner, mocker):
    planner = pgo_planner
    events = mocker.Mock()
    for name in ("handle_global_costmap", "handle_odom", "handle_goal_request"):
        events.attach_mock(getattr(planner._planner, name), name)
    assert planner.set_tagged_goal("ext-1", PoseStamped(position=[1, 2, 0], frame_id="world"))
    revision = planner.pause_for_pgo()
    planner._on_goal_finished(Bool(False))
    events.reset_mock()
    aligned_odom = PoseStamped(position=[2.5, 0, 0.4], frame_id="world", ts=12.0)
    corrected_map = cloud([[2.5, 0, 0]])

    assert planner.resume_after_pgo(revision, corrected_map, aligned_odom)

    assert [call[0] for call in events.mock_calls] == [
        "handle_global_costmap",
        "handle_odom",
        "handle_goal_request",
    ]
    planner._costmapper.calculate_navigation_costmap.assert_called_once_with(corrected_map)
    goal = planner._planner.handle_goal_request.call_args.args[0]
    assert goal.position.to_numpy().tolist() == [3, 4, 0.4]
    assert goal.orientation.to_euler().z == pytest.approx(0.5)
    assert goal.frame_id == "world"
    planner._planner.cancel_goal.assert_called_once()
    assert planner.navigation_state.publish.call_args.args[0].data == (
        "PGO correction: navigation replanned"
    )
    # Another loop refreshes from the database, not the previously corrected goal.
    planner._spatial_memory.get_robot_locations.return_value[0].position = (5, 6, 1)
    assert planner.resume_after_pgo(planner.pause_for_pgo(), corrected_map, aligned_odom)
    assert planner._planner.handle_goal_request.call_args.args[0].position.x == 5


def test_live_loop_saves_corrected_map_and_resumes_active_tag_navigation(
    session, pgo_planner, tmp_path, mocker
):
    path = tmp_path / "office.pc2.lcm"
    save_premap(path)
    pgo = mocker.patch.object(persistent, "PGOMap").return_value
    pgo.add.return_value = True
    pgo.graph.return_value = fixed_graph()
    pgo.global_map.return_value = cloud([[2.5, 0, 0]])
    module = session(pgo_enabled=True)
    module._candidate = Transform(frame_id="world", child_frame_id="world")
    module.confirm_alignment()
    module._on_odom(PoseStamped(frame_id="world", position=[2, 0, 0], ts=12.0))
    mocker.patch.object(module, "_pgo_navigation", pgo_planner)
    pgo_planner.set_tagged_goal("ext-1", PoseStamped(position=[1, 2, 0], frame_id="world"))

    def update_tag_after_loop(_graph, _session, _cloud):
        pgo_planner._spatial_memory.get_robot_locations.return_value[0].position = (7, 8, 1)

    module._pgo_memory.update_pgo_graph.side_effect = update_tag_after_loop
    resume = mocker.spy(pgo_planner, "resume_after_pgo")

    def check_checkpoint_before_planning(_cloud):
        saved = PointCloud2.lcm_decode(path.read_bytes())
        np.testing.assert_allclose(saved.points_f32()[-1], [2.5, 0, 0])
        return OccupancyGrid(grid=np.zeros((20, 20), dtype=np.int8), ts=12.0)

    pgo_planner._costmapper.calculate_navigation_costmap.side_effect = (
        check_checkpoint_before_planning
    )

    module._on_lidar(cloud([[2, 0, 0]]))

    assert resume.spy_return is True
    goal = pgo_planner._planner.handle_goal_request.call_args.args[0]
    assert goal.position.x == 7
    assert goal.position.y == 8
    assert pgo_planner._planner.handle_odom.call_args.args[0].position.x == 2.5
    assert module.navigation_ready()
    assert not module._pgo_paused
    module.pgo_stop.publish.assert_not_called()


@pytest.mark.parametrize("interruption", ["cancel", "new-goal", "teleop-stop", "arrived"])
def test_pgo_never_resurrects_cancelled_replaced_or_finished_navigation(pgo_planner, interruption):
    planner = pgo_planner
    planner.set_tagged_goal("ext-1", PoseStamped(position=[1, 2, 0], frame_id="world"))
    if interruption == "arrived":
        planner._on_goal_finished(Bool(True))
    revision = planner.pause_for_pgo()
    if interruption == "cancel":
        planner.cancel_goal()
    elif interruption == "new-goal":
        assert not planner.set_goal(PoseStamped(position=[8, 9, 0], frame_id="world"))
    elif interruption == "teleop-stop":
        planner._on_stop_movement(Bool(True))
    planner._planner.handle_goal_request.reset_mock()

    assert not planner.resume_after_pgo(revision, cloud([[2, 0, 0]]), PoseStamped())

    planner._planner.handle_goal_request.assert_not_called()
    planner._costmapper.calculate_navigation_costmap.assert_not_called()


def test_cancel_during_pgo_costmap_refresh_wins_over_automatic_resume(pgo_planner):
    planner = pgo_planner
    planner.set_tagged_goal("ext-1", PoseStamped(position=[1, 2, 0], frame_id="world"))
    revision = planner.pause_for_pgo()
    planner._planner.handle_goal_request.reset_mock()

    def cancel_while_refreshing(_cloud):
        planner.cancel_goal()
        return OccupancyGrid()

    planner._costmapper.calculate_navigation_costmap.side_effect = cancel_while_refreshing

    assert not planner.resume_after_pgo(revision, cloud([[2, 0, 0]]), PoseStamped())

    planner._planner.handle_goal_request.assert_not_called()


@pytest.mark.parametrize("failure", ["missing-tag", "no-odom", "database", "costmap"])
def test_pgo_refresh_failure_stops_without_reusing_stale_goal(pgo_planner, failure):
    planner = pgo_planner
    planner.set_tagged_goal("ext-1", PoseStamped(position=[1, 2, 0], frame_id="world"))
    revision = planner.pause_for_pgo()
    planner._planner.handle_goal_request.reset_mock()
    odom = PoseStamped()
    if failure == "missing-tag":
        planner._spatial_memory.get_robot_locations.return_value = []
    elif failure == "no-odom":
        odom = None
    elif failure == "database":
        planner._spatial_memory.get_robot_locations.side_effect = RuntimeError(
            "database unavailable"
        )
    else:
        planner._costmapper.calculate_navigation_costmap.side_effect = RuntimeError(
            "costmap unavailable"
        )

    with pytest.raises(RuntimeError):
        planner.resume_after_pgo(revision, cloud([[2, 0, 0]]), odom)

    planner._planner.handle_goal_request.assert_not_called()
    assert planner._active_goal is None
    assert planner.navigation_state.publish.call_args.args[0].data == (
        "PGO navigation refresh failed; stopped"
    )


def test_pgo_world_goal_stays_fixed_and_old_costmaps_cannot_replace_corrected_map(pgo_planner):
    planner = pgo_planner
    planner.set_goal(PoseStamped(position=[1, 2, 0], frame_id="world"))
    revision = planner.pause_for_pgo()
    old_map = OccupancyGrid(ts=11.0)
    planner._handle_global_costmap(old_map)
    planner._planner.handle_global_costmap.assert_not_called()
    assert planner.resume_after_pgo(revision, cloud([[2, 0, 0]]), PoseStamped())
    planner._spatial_memory.get_robot_locations.assert_not_called()
    assert planner._planner.handle_goal_request.call_args.args[0].position.to_numpy().tolist() == [
        1,
        2,
        0,
    ]
    planner._planner.handle_global_costmap.reset_mock()
    planner._handle_global_costmap(old_map)
    planner._planner.handle_global_costmap.assert_not_called()
    updated_map = OccupancyGrid(ts=13.0)
    planner._handle_global_costmap(updated_map)
    planner._planner.handle_global_costmap.assert_called_once_with(updated_map)


@pytest.mark.parametrize("distance,arrived", [(1.01, False), (1.0, True), (0.99, True)])
def test_nearby_navigation_stops_at_one_meter_without_matching_height_or_yaw(
    pgo_planner, distance, arrived
):
    planner = pgo_planner
    assert planner.set_nearby_tagged_goal(
        "ext-1", PoseStamped(position=[3, 4, 99], frame_id="world")
    )
    planner._handle_odom(PoseStamped(position=[3 - distance, 4, 0], frame_id="world"))
    assert planner._planner.cancel_goal.call_count == int(arrived)
    if arrived:
        planner._planner.cancel_goal.assert_called_once_with(arrived=True)
        assert planner.nav_cmd_vel.publish.call_args.args[0].linear.x == 0
    else:
        assert planner._active_goal is not None


def test_nearby_already_in_radius_does_not_start_moving(pgo_planner, mocker):
    planner = pgo_planner
    mocker.patch.object(
        planner._planner, "_current_odom", PoseStamped(position=[2.5, 4, 0], frame_id="world")
    )
    assert planner.set_nearby_tagged_goal(
        "ext-1", PoseStamped(position=[3, 4, 0], frame_id="world")
    )
    planner._planner.handle_goal_request.assert_not_called()
    planner._planner.cancel_goal.assert_called_once_with(arrived=True)


@pytest.mark.parametrize("radius", [0.3, 1.7, 3.0])
def test_configurable_nearby_radius_stops_at_exact_threshold(pgo_planner, radius):
    planner = pgo_planner
    assert planner.set_nearby_arrival_distance(radius) == {"distance_m": radius}
    planner.set_nearby_tagged_goal("ext-1", PoseStamped(position=[3, 4, 0], frame_id="world"))
    planner._handle_odom(PoseStamped(position=[3 - radius - 0.01, 4, 0], frame_id="world"))
    planner._planner.cancel_goal.assert_not_called()
    planner._handle_odom(PoseStamped(position=[3 - radius, 4, 0], frame_id="world"))
    planner._planner.cancel_goal.assert_called_once_with(arrived=True)
    assert (
        planner.navigation_state.publish.call_args.args[0].data == "Arrived within nearby threshold"
    )


def test_slider_change_keeps_active_radius_and_pgo_preserves_it(pgo_planner):
    planner = pgo_planner
    planner.set_nearby_arrival_distance(0.7)
    planner.set_nearby_tagged_goal("ext-1", PoseStamped(position=[3, 4, 0], frame_id="world"))
    planner.set_nearby_arrival_distance(2.0)
    revision = planner.pause_for_pgo()
    assert planner.resume_after_pgo(
        revision, cloud([[2, 0, 0]]), PoseStamped(position=[1, 4, 0], frame_id="world")
    )
    assert planner._arrival_radius == 0.7
    planner._planner.cancel_goal.reset_mock()
    planner._handle_odom(PoseStamped(position=[2, 4, 0], frame_id="world"))
    planner._planner.cancel_goal.assert_not_called()
    planner._handle_odom(PoseStamped(position=[2.3, 4, 0], frame_id="world"))
    planner._planner.cancel_goal.assert_called_once_with(arrived=True)


@pytest.mark.parametrize("radius", [float("nan"), float("inf"), 0.2, 3.1])
def test_invalid_nearby_radius_is_rejected_without_changing_threshold(pgo_planner, radius):
    with pytest.raises(ValueError, match="between"):
        pgo_planner.set_nearby_arrival_distance(radius)
    assert pgo_planner.nearby_navigation_status() == {"distance_m": 1.0}


def test_core_safe_goal_completion_outside_radius_does_not_claim_arrival(pgo_planner, mocker):
    planner = pgo_planner
    mocker.patch.object(planner._planner, "_current_odom", PoseStamped(position=[0, 0, 0]))
    planner.set_nearby_tagged_goal("ext-1", PoseStamped(position=[3, 4, 0], frame_id="world"))
    planner._on_goal_finished(Bool(True))
    assert planner.navigation_state.publish.call_args.args[0].data == (
        "Navigation stopped outside nearby threshold; target may be unreachable"
    )
    assert planner._active_goal is None


def test_precise_navigation_does_not_inherit_nearby_radius(pgo_planner):
    planner = pgo_planner
    planner.set_nearby_tagged_goal("ext-1", PoseStamped(position=[3, 4, 0], frame_id="world"))
    planner.set_tagged_goal("ext-1", PoseStamped(position=[3, 4, 0], frame_id="world"))
    planner._handle_odom(PoseStamped(position=[2.5, 4, 0], frame_id="world"))
    planner._planner.cancel_goal.assert_not_called()
    assert planner._arrival_radius is None


def test_pgo_updates_nearby_tag_and_keeps_one_meter_arrival_rule(pgo_planner):
    planner = pgo_planner
    planner.set_nearby_tagged_goal("ext-1", PoseStamped(position=[99, 99, 0], frame_id="world"))
    revision = planner.pause_for_pgo()
    planner._planner.cancel_goal.reset_mock()
    planner._planner.handle_goal_request.reset_mock()
    assert planner.resume_after_pgo(
        revision, cloud([[2, 0, 0]]), PoseStamped(position=[2.5, 4, 0], frame_id="world")
    )
    planner._planner.cancel_goal.assert_called_once_with(arrived=True)
    planner._planner.handle_goal_request.assert_not_called()


def test_original_planner_keeps_existing_goal_behavior(mocker):
    planner = ReplanningAStarPlanner()
    try:
        handle = mocker.patch.object(planner._planner, "handle_goal_request")
        goal = PoseStamped(position=[1, 0, 0])
        assert planner.set_goal(goal) is True
        handle.assert_called_once_with(goal)
    finally:
        planner.dispose()


def test_stream_navigation_goals_use_the_same_confirmation_gate(mocker):
    planner = PersistentGo2Planner()
    try:
        mocker.patch.object(Module, "start")
        mocker.patch.object(planner._planner, "start")
        map_session = mocker.patch.object(planner, "_map_session", create=True)
        map_session.navigation_ready.return_value = False
        handle = mocker.patch.object(planner._planner, "handle_goal_request")
        subscriptions = {
            name: mocker.patch.object(port, "subscribe", return_value=lambda: None)
            for name, port in planner.inputs.items()
        }
        planner.start()
        goal = PoseStamped(frame_id="world", position=[1, 0, 0])
        for name in ("goal_request", "target"):
            subscriptions[name].call_args.args[0](goal)
        subscriptions["clicked_point"].call_args.args[0](PointStamped(x=1, frame_id="world"))
        handle.assert_not_called()
    finally:
        planner.stop()
        planner.dispose()


def test_persistent_blueprint_routes_all_world_data_through_alignment_gate():
    blueprint = unitree_go2_agentic_persistent
    _verify_no_name_conflicts(blueprint)
    modules = {atom.module for atom in blueprint.active_blueprints}
    assert PersistentGo2Map in modules
    assert PersistentGo2Planner in modules
    assert VoxelGridMapper not in modules
    assert ReplanningAStarPlanner not in modules
    assert VoxelGridMapper in {atom.module for atom in unitree_go2_agentic.active_blueprints}
    producers = {}
    for atom in blueprint.active_blueprints:
        for port in atom.streams:
            if port.direction == "out":
                name = blueprint.remapping_map.get((atom.name, port.name), port.name)
                producers.setdefault(name, []).append(atom.module)
    for name in ("lidar", "odom", "tf", "global_map", "cmd_vel"):
        assert producers[name] == [PersistentGo2Map]
    for name in ("session_lidar", "session_odom", "session_tf"):
        assert producers[name] == [GO2Connection]
    for atom in blueprint.active_blueprints:
        if atom.module is RerunBridgeModule:
            assert "tf" in atom.kwargs["topics"]
            assert "alignment_preview" in atom.kwargs["topics"]
            assert not {"session_lidar", "session_odom", "session_tf"} & set(atom.kwargs["topics"])

    for atom in blueprint.active_blueprints:
        for ref in atom.module_refs:
            expected = {
                "_pgo_navigation": "persistentgo2planner",
                "_tagged_navigation": "persistentgo2planner",
                "_costmapper": "costmapper",
                "_spatial_memory": "spatialmemory",
            }
            if ref.name in expected:
                assert (
                    _resolve_single_ref(
                        atom, ref, ref.spec, blueprint, set(blueprint.disabled_modules_tuple)
                    )
                    == expected[ref.name]
                )
            if ref.name == "_map_session":
                assert (
                    _resolve_single_ref(
                        atom, ref, ref.spec, blueprint, set(blueprint.disabled_modules_tuple)
                    )
                    == "persistentgo2map"
                )
            if ref.name == "_navigation":
                assert (
                    _resolve_single_ref(
                        atom, ref, ref.spec, blueprint, set(blueprint.disabled_modules_tuple)
                    )
                    == "persistentgo2planner"
                )


def test_documented_cli_flags_select_map_and_creation_mode():
    parsed = BlueprintConfigParser(unitree_go2_agentic_persistent).parse(
        [
            "--persistentgo2map.map-file=assets/scene_maps/office/map.pc2.lcm",
            "--persistentgo2map.create-new=true",
            "--spatialmemory.scene-map-dir=assets/scene_maps/office",
            "--mcpclient.model=gpt-4o-mini",
        ],
        environ={},
    )
    config = parsed.module_kwargs("persistentgo2map")
    assert config["map_file"] == "assets/scene_maps/office/map.pc2.lcm"
    assert config["create_new"] is True


def test_rotation_collects_whole_sweep_and_stops_before_matching(session, tmp_path, mocker):
    save_premap(tmp_path / "office.pc2.lcm")
    clock = mocker.patch.object(persistent.time, "monotonic", return_value=100.0)
    # Drive the timer explicitly, without any real-time background callbacks.
    timer = mocker.patch.object(persistent, "interval")
    timer.return_value.subscribe.return_value = mocker.Mock(dispose=mocker.Mock())
    module = session(startup_rotation=True, rotation_duration=5.0)
    module._rotation_tick()
    module.cmd_vel.publish.assert_not_called()
    module._on_odom(PoseStamped(frame_id="world", position=[0, 0, 0]))
    module._on_lidar(cloud([[0, 0, 0]]))
    module._rotation_tick()
    rotating = module.cmd_vel.publish.call_args.args[0]
    assert rotating.linear.to_numpy().tolist() == [0, 0, 0]
    assert rotating.angular.z == 0.15
    module._match(cloud([[0, 0, 0]]))
    module._relocalizer.relocalize.assert_not_called()
    # Far more than seven scans: the first region must survive the full sweep.
    for index in range(1, 15):
        module._on_lidar(cloud([[index, 0, 0]]))
    captured = mocker.patch.object(module._scans, "on_next")

    def check_stopped(_cloud):
        assert module.cmd_vel.publish.call_args.args[0].angular.z == 0

    captured.side_effect = check_stopped
    clock.return_value = 105.0
    module._on_odom(PoseStamped(frame_id="world", position=[0, 0, 0]))
    module._on_lidar(cloud([[15, 0, 0]]))
    module._rotation_tick()
    stopped = module.cmd_vel.publish.call_args.args[0]
    assert stopped.linear.to_numpy().tolist() == [0, 0, 0]
    assert stopped.angular.to_numpy().tolist() == [0, 0, 0]
    snapshot = captured.call_args.args[0]
    assert len(snapshot) == 16
    assert np.any(np.linalg.norm(snapshot.as_numpy()[0] - [0, 0, 0], axis=1) < 0.1)
    assert not module.navigation_ready()
    module._on_lidar(cloud([[99, 0, 0]]))
    assert captured.call_args.args[0] is snapshot


@pytest.mark.parametrize(
    "reason", ["sensor-loss", "cancel", "shutdown", "manual-command", "stop-message"]
)
def test_rotation_stops_on_sensor_loss_cancel_shutdown_or_override(
    session, tmp_path, mocker, reason
):
    save_premap(tmp_path / "office.pc2.lcm")
    clock = mocker.patch.object(persistent.time, "monotonic", return_value=100.0)
    timer = mocker.patch.object(persistent, "interval")
    timer.return_value.subscribe.return_value = mocker.Mock(dispose=mocker.Mock())
    module = session(startup_rotation=True)
    module._on_odom(PoseStamped(frame_id="world", position=[0, 0, 0]))
    module._on_lidar(cloud([[0, 0, 0]]))
    module._rotation_tick()
    if reason == "sensor-loss":
        clock.return_value = 102.0
        module._rotation_tick()
    elif reason == "cancel":
        module.cancel_startup_rotation()
    elif reason == "shutdown":
        module.stop()
    elif reason == "stop-message":
        message = Bool()
        message.data = True
        module._on_stop_movement(message)
    else:
        module._on_cmd_vel(Twist(angular=Vector3(0, 0, -0.1)))
    commands = [call.args[0] for call in module.cmd_vel.publish.call_args_list]
    assert any(
        cmd.linear.to_numpy().tolist() == [0, 0, 0] and cmd.angular.to_numpy().tolist() == [0, 0, 0]
        for cmd in commands
    )
    assert module._rotation_aborted
    assert not module.navigation_ready()
    count = module.cmd_vel.publish.call_count
    module._rotation_tick()
    assert module.cmd_vel.publish.call_count == count


def test_disabled_rotation_preserves_normal_velocity_forwarding(session):
    module = session(create_new=True)
    command = Twist(angular=Vector3(0, 0, 0.1))
    module._on_cmd_vel(command)
    module.cmd_vel.publish.assert_called_once_with(command)


def test_new_map_does_not_rotate_even_when_enabled(session, mocker):
    timer = mocker.patch.object(persistent, "interval")
    module = session(create_new=True, startup_rotation=True)
    module._on_odom(PoseStamped(frame_id="world", position=[0, 0, 0]))
    module._on_lidar(cloud([[0, 0, 0]]))
    module._rotation_tick()
    timer.assert_not_called()
    module.cmd_vel.publish.assert_not_called()
    assert module.navigation_ready()


def test_rotation_publication_failure_stops_and_blocks_retry(session, tmp_path, mocker):
    save_premap(tmp_path / "office.pc2.lcm")
    mocker.patch.object(persistent.time, "monotonic", return_value=100.0)
    timer = mocker.patch.object(persistent, "interval")
    timer.return_value.subscribe.return_value = mocker.Mock(dispose=mocker.Mock())
    module = session(startup_rotation=True)
    module._on_odom(PoseStamped(frame_id="world", position=[0, 0, 0]))
    module._on_lidar(cloud([[0, 0, 0]]))
    module.cmd_vel.publish.side_effect = [RuntimeError("transport unavailable"), None]
    module._rotation_tick()
    assert module.cmd_vel.publish.call_args.args[0].angular.z == 0
    assert module._rotation_aborted
    assert not module.navigation_ready()
    module._match(cloud([[0, 0, 0]]))
    module._relocalizer.relocalize.assert_not_called()


@pytest.mark.parametrize(
    "config",
    [
        {"rotation_speed": 0.5},
        {"rotation_speed": float("nan")},
        {"rotation_duration": 31},
        {"sensor_timeout": 0},
    ],
)
def test_rotation_rejects_unsafe_configuration(session, tmp_path, config):
    save_premap(tmp_path / "office.pc2.lcm")
    with pytest.raises(ValueError, match="Rotation requires"):
        session(startup_rotation=True, **config)


def test_manual_capture_accumulates_walk_and_turn_without_automatic_motion(
    session, tmp_path, mocker
):
    save_premap(tmp_path / "office.pc2.lcm")
    module = session(manual_capture=True)
    captured = mocker.patch.object(module._scans, "on_next")
    for index in range(16):
        module._on_lidar(cloud([[index, 0, 0]]))
    module._match(cloud([[0, 0, 0]]))
    module._relocalizer.relocalize.assert_not_called()
    captured.assert_not_called()
    module.cmd_vel.publish.assert_not_called()
    command = Twist(linear=Vector3(0.1, 0, 0), angular=Vector3(0, 0, 0.1))
    module._on_cmd_vel(command)
    module.cmd_vel.publish.assert_called_once_with(command)
    assert "16 scans" in module.alignment_status()

    module.finish_startup_capture()

    snapshot = captured.call_args.args[0]
    assert len(snapshot) == 16
    stopped = module.cmd_vel.publish.call_args.args[0]
    assert stopped.linear.to_numpy().tolist() == [0, 0, 0]
    assert stopped.angular.to_numpy().tolist() == [0, 0, 0]
    assert not module.navigation_ready()
    module._on_lidar(cloud([[99, 0, 0]]))
    assert captured.call_args.args[0] is snapshot
    with pytest.raises(RuntimeError, match="No manual startup capture"):
        module.finish_startup_capture()


def test_empty_manual_capture_can_continue_after_finish_refused(session, tmp_path):
    save_premap(tmp_path / "office.pc2.lcm")
    module = session(manual_capture=True)
    with pytest.raises(RuntimeError, match="Not enough captured points"):
        module.finish_startup_capture()
    module._on_lidar(cloud([[1, 0, 0]]))
    assert "1 scans" in module.alignment_status()
    assert not module.navigation_ready()


def test_capture_modes_are_mutually_exclusive(session):
    with pytest.raises(ValueError, match="cannot both be enabled"):
        session(manual_capture=True, startup_rotation=True)


def test_new_map_ignores_manual_capture(session):
    module = session(create_new=True, manual_capture=True)
    assert module.navigation_ready()
    with pytest.raises(RuntimeError, match="No manual startup capture"):
        module.finish_startup_capture()
