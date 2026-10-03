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
from dimos.msgs.sensor_msgs.PointCloud2 import PointCloud2
from dimos.msgs.tf2_msgs.TFMessage import TFMessage
from dimos.navigation.go2.replanning_a_star.module import ReplanningAStarPlanner
from dimos.robot.unitree.go2.blueprints.agentic.unitree_go2_agentic import unitree_go2_agentic
from dimos.robot.unitree.go2.blueprints.agentic.unitree_go2_agentic_persistent import (
    unitree_go2_agentic_persistent,
)
from dimos.robot.unitree.go2.connection import GO2Connection
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


def save_premap(path):
    prior = cloud([[0, 0, 0], [4, 2, 0], [4, 2, 1]])
    path.write_bytes(prior.lcm_encode())
    return prior


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
