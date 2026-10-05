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

"""Supervised cross-session mapping in a stable world coordinate system."""

import copy
import math
import os
from pathlib import Path
import tempfile
from threading import Event, RLock, Thread, current_thread
import time
from typing import Any

from dimos_lcm.std_msgs import Bool, String
import numpy as np
from pydantic import Field
from reactivex import Subject, interval, operators as ops
from reactivex.disposable import Disposable

from dimos.constants import DEFAULT_THREAD_JOIN_TIMEOUT
from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.core.stream import In, Out
from dimos.mapping.costmapper import CostMapper
from dimos.mapping.relocalization.go2.fusion_gate import FusionGateConfig, FusionMotionGate
from dimos.mapping.relocalization.lidar.module import window
from dimos.mapping.relocalization.lidar.relocalize import MID360, LidarRelocalizer, RelocalizeConfig
from dimos.mapping.voxels.grid import VoxelGrid
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Quaternion import Quaternion
from dimos.msgs.geometry_msgs.Transform import Transform
from dimos.msgs.geometry_msgs.Twist import Twist
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.nav_msgs.OccupancyGrid import OccupancyGrid
from dimos.msgs.sensor_msgs.PointCloud2 import PointCloud2
from dimos.msgs.tf2_msgs.TFMessage import TFMessage
from dimos.navigation.base import NavigationState
from dimos.navigation.go2.loop_closure.memory_spec import (
    PGOMemorySpec,
    PGONavigationSpec,
    TagViewSpec,
)
from dimos.navigation.go2.loop_closure.pgo import PoseGraph
from dimos.navigation.go2.loop_closure.pgo_map import PGOMap
from dimos.navigation.go2.replanning_a_star.module import (
    ReplanningAStarPlanner,
    ReplanningAStarPlannerConfig,
)
from dimos.perception.experimental.spatial_memory_spec import SpatialMemorySpec
from dimos.robot.unitree.type.lidar import repair_stale_ts
from dimos.utils.logging_config import setup_logger
from dimos.utils.reactive import backpressure

logger = setup_logger()


class PersistentGo2MapConfig(ModuleConfig, FusionGateConfig):
    map_file: str | None = None
    create_new: bool = False
    voxel_size: float = 0.05
    emit_every: int = 5
    save_interval: float = 30.0
    reloc_interval: float = 2.0
    min_local_points: int = 2_000
    relocalize: RelocalizeConfig = MID360
    startup_rotation: bool = False
    manual_capture: bool = False
    rotation_speed: float = 0.15
    rotation_duration: float = 20.0
    sensor_timeout: float = 1.0
    pgo_enabled: bool = False


class PersistentGo2Map(Module):
    """Gate live sensors until a human approves their placement in a saved map."""

    config: PersistentGo2MapConfig
    session_lidar: In[PointCloud2]
    session_odom: In[PoseStamped]
    session_tf: In[TFMessage]
    session_cmd_vel: In[Twist]
    stop_movement: In[Bool]
    cmd_vel: Out[Twist]
    lidar: Out[PointCloud2]
    odom: Out[PoseStamped]
    tf: Out[TFMessage]
    pgo_raw_tf: Out[TFMessage]
    pgo_raw_lidar: Out[PointCloud2]
    global_map: Out[PointCloud2]
    alignment_preview: Out[PointCloud2]
    alignment_scan: Out[PointCloud2]
    pgo_stop: Out[Bool]
    _pgo_memory: PGOMemorySpec | None = None
    _pgo_navigation: PGONavigationSpec | None = None

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._lock = RLock()
        self._scans: Subject[PointCloud2] = Subject()
        self._grid: VoxelGrid | None = None
        self._relocalizer: LidarRelocalizer | None = None
        self._premap: PointCloud2 | None = None
        self._candidate: Transform | None = None
        self._placement: Transform | None = None
        self._latest_odom: PoseStamped | None = None
        self._latest_tf: TFMessage | None = None
        self._frames = 0
        self._stopping = False
        self._capture_grid: VoxelGrid | None = None
        self._capture_cloud: PointCloud2 | None = None
        self._rotation_started: float | None = None
        self._rotation_armed = False
        self._rotation_aborted = False
        self._last_lidar_rx: float | None = None
        self._last_odom_rx: float | None = None
        self._capture_frames = 0
        self._manual_capture_active = False
        self._pgo: PGOMap | None = None
        self._pgo_graph: PoseGraph | None = None
        self._pgo_error: str | None = None
        self._pgo_paused = False
        self._pgo_session = f"{time.time_ns()}"
        self._fusion_gate = FusionMotionGate(self.config)
        self._fusion_manual_paused = False
        self._fusion_skipped = 0
        self._fusion_last_reason: str | None = None

    @rpc
    def start(self) -> None:
        super().start()
        if self.config.map_file is None:
            raise ValueError("PersistentGo2Map requires map_file")
        path = Path(self.config.map_file)
        if self.config.emit_every < 1 or self.config.save_interval < 0:
            raise ValueError("emit_every must be positive and save_interval must be nonnegative")
        if self.config.manual_capture and self.config.startup_rotation:
            raise ValueError("manual_capture and startup_rotation cannot both be enabled")
        if self.config.startup_rotation:
            if (
                not math.isfinite(self.config.rotation_speed)
                or not 0 < abs(self.config.rotation_speed) <= 0.3
                or not math.isfinite(self.config.rotation_duration)
                or not 0 < self.config.rotation_duration <= 30
                or not math.isfinite(self.config.sensor_timeout)
                or not 0 < self.config.sensor_timeout <= 2
            ):
                raise ValueError(
                    "Rotation requires 0 < |speed| <= 0.3 rad/s, "
                    "0 < duration <= 30 s, and 0 < sensor_timeout <= 2 s"
                )
        if self.config.create_new and path.exists():
            raise FileExistsError(f"Refusing to replace existing map: {path}")
        if not self.config.create_new and not path.is_file():
            raise FileNotFoundError(f"Saved map not found: {path}; use create_new for a first run")
        if self.config.pgo_enabled:
            if self._pgo_memory is None:
                raise ValueError("PGO requires SpatialMemory for synchronized tag corrections")
            if self._pgo_navigation is None:
                raise ValueError("PGO requires PersistentGo2Planner for synchronized replanning")
            self._pgo = PGOMap(
                voxel_size=self.config.voxel_size, fixed_world=True, rebuild_cooldown_s=0
            )
            self._pgo_memory.update_pgo_graph(PoseGraph(), self._pgo_session)

        self._grid = VoxelGrid(voxel_size=self.config.voxel_size, frame_id="world")
        if self.config.create_new:
            self._placement = Transform.from_matrix(
                np.eye(4), frame_id="world", child_frame_id="world"
            )
            logger.info("Creating persistent map; this session defines the saved world frame")
        else:
            self._premap = PointCloud2.lcm_decode(path.read_bytes())
            if self._premap.frame_id != "world" or len(self._premap) == 0:
                raise ValueError("Saved map must be a nonempty point cloud in the world frame")
            self._grid.add_frame(self._premap)
            logger.warning(
                "Restored map: navigation and sensor forwarding are blocked until "
                "confirm_alignment(). Go2 built-in lidar matching is experimental; "
                "the current matching preset was measured on MID360."
            )
            self._relocalizer = LidarRelocalizer(self._premap.pointcloud, self.config.relocalize)
            if self.config.startup_rotation or self.config.manual_capture:
                self._capture_grid = VoxelGrid(
                    voxel_size=self.config.voxel_size, frame_id="world", carve_columns=False
                )
                self._rotation_armed = self.config.startup_rotation
                self._manual_capture_active = self.config.manual_capture
                matching = self._scans.pipe(ops.throttle_first(self.config.reloc_interval))
                if self.config.manual_capture:
                    logger.info(
                        "Manual capture enabled: drive and turn under supervision, stop "
                        "the robot, then call finish_startup_capture() to begin matching."
                    )
                else:
                    logger.warning(
                        "Startup rotation enabled: robot will turn in place after fresh lidar "
                        "and odometry arrive. Keep its turning footprint clear; no automatic "
                        "obstacle avoidance is provided during this capture."
                    )
            else:
                matching = window(self._scans, self.config.relocalize, self.config.reloc_interval)
            self.register_disposable(
                backpressure(matching).subscribe(self._match, on_error=self._on_match_error)
            )
        self.register_disposable(Disposable(self.session_odom.subscribe(self._on_odom)))
        self.register_disposable(Disposable(self.session_tf.subscribe(self._on_tf)))
        if self._pgo is not None:
            self.register_disposable(
                backpressure(self.session_lidar.observable().pipe(repair_stale_ts())).subscribe(
                    self._on_lidar, on_error=self._on_pgo_stream_error
                )
            )
        else:
            self.register_disposable(Disposable(self.session_lidar.subscribe(self._on_lidar)))
        self.register_disposable(Disposable(self.session_cmd_vel.subscribe(self._on_cmd_vel)))
        self.register_disposable(Disposable(self.stop_movement.subscribe(self._on_stop_movement)))
        if self._rotation_armed:
            self.register_disposable(interval(0.1).subscribe(lambda _: self._rotation_tick()))
        if self.config.save_interval > 0:
            self.register_disposable(
                interval(self.config.save_interval).subscribe(lambda _: self._autosave())
            )

    def _on_match_error(self, error: Exception) -> None:
        logger.error("Persistent map matching stream failed", error=str(error))

    def _on_cmd_vel(self, command: Twist) -> None:
        with self._lock:
            if self._stopping:
                return
            if self.config.auto_pause_fusion:
                self._fusion_gate.on_command(command, time.monotonic())
            if self._pgo_error is not None or self._pgo_paused:
                self.cmd_vel.publish(Twist())
                return
            if self._rotation_armed or self._rotation_started is not None:
                moving = any(
                    value != 0
                    for value in (
                        command.linear.x,
                        command.linear.y,
                        command.linear.z,
                        command.angular.x,
                        command.angular.y,
                        command.angular.z,
                    )
                )
                if not moving:
                    return
                self._cancel_rotation("Another movement command interrupted startup capture")
            self.cmd_vel.publish(command)

    def _on_stop_movement(self, message: Bool) -> None:
        with self._lock:
            if message.data and (self._rotation_armed or self._rotation_started is not None):
                self._cancel_rotation("Stop movement request cancelled startup rotation")

    def _cancel_rotation(self, reason: str) -> None:
        self._rotation_armed = False
        self._rotation_started = None
        self._rotation_aborted = True
        self.cmd_vel.publish(Twist())
        logger.warning(reason)

    @rpc
    def cancel_startup_rotation(self) -> str:
        """Stop startup capture without approving map alignment; restart to retry."""
        with self._lock:
            if not self._rotation_armed and self._rotation_started is None:
                return "No startup rotation is active."
            self._cancel_rotation("Human cancelled startup rotation")
        return "Stopped. Map alignment remains blocked; restart to retry capture."

    @rpc
    def finish_startup_capture(self) -> str:
        """Finish manual scan collection after stopping the robot; begin matching."""
        with self._lock:
            if self._stopping or not self._manual_capture_active:
                raise RuntimeError("No manual startup capture is active")
            assert self._capture_grid is not None
            cloud = self._capture_grid.get_global_pointcloud2()
            if len(cloud) < self.config.min_local_points:
                raise RuntimeError(
                    f"Not enough captured points: {len(cloud)}; "
                    f"need {self.config.min_local_points}. Continue manual capture."
                )
            self.cmd_vel.publish(Twist())
            self.alignment_scan.publish(cloud)
            self._capture_cloud = cloud
            self._capture_grid.dispose()
            self._capture_grid = None
            self._manual_capture_active = False
            self._scans.on_next(cloud)
        logger.info("Manual scan capture finished; matching accumulated cloud", n_points=len(cloud))
        return (
            "Capture finished. Waiting for a candidate; human alignment approval is still required."
        )

    def _rotation_tick(self) -> None:
        try:
            self._advance_rotation()
        except (RuntimeError, OSError, ValueError):
            with self._lock:
                self._rotation_armed = False
                self._rotation_started = None
                self._rotation_aborted = True
                logger.exception("Startup rotation capture failed")
                self.cmd_vel.publish(Twist())

    def _advance_rotation(self) -> None:
        with self._lock:
            if self._stopping or self._rotation_aborted:
                return
            if not self._rotation_armed and self._rotation_started is None:
                return
            now = time.monotonic()
            fresh = (
                self._last_lidar_rx is not None
                and self._last_odom_rx is not None
                and now - self._last_lidar_rx <= self.config.sensor_timeout
                and now - self._last_odom_rx <= self.config.sensor_timeout
            )
            if not fresh:
                if self._rotation_started is not None:
                    self._cancel_rotation("Startup rotation stopped: lidar or odometry is stale")
                return
            if self._rotation_started is None:
                self._rotation_armed = False
                self._rotation_started = now
                logger.warning("Starting bounded in-place scan capture")
            if now - self._rotation_started >= self.config.rotation_duration:
                self.cmd_vel.publish(Twist())
                self._rotation_started = None
                assert self._capture_grid is not None
                self._capture_cloud = self._capture_grid.get_global_pointcloud2()
                self._capture_grid.dispose()
                self._capture_grid = None
                logger.info(
                    "Startup rotation stopped; matching accumulated cloud",
                    n_points=len(self._capture_cloud),
                )
                self.alignment_scan.publish(self._capture_cloud)
                self._scans.on_next(self._capture_cloud)
                return
            self.cmd_vel.publish(
                Twist(linear=Vector3(), angular=Vector3(0, 0, self.config.rotation_speed))
            )

    def _match(self, cloud: PointCloud2) -> None:
        with self._lock:
            if (
                self._stopping
                or self._placement is not None
                or self._candidate is not None
                or self._rotation_armed
                or self._rotation_started is not None
                or self._rotation_aborted
                or self._manual_capture_active
            ):
                return
        if len(cloud) < self.config.min_local_points:
            logger.info(
                "Waiting for enough lidar points to match the saved map", n_points=len(cloud)
            )
            return
        assert self._relocalizer is not None
        try:
            candidate = self._relocalizer.relocalize(cloud.pointcloud, "world", "map")
        except (RuntimeError, ValueError):
            logger.exception("Persistent map alignment failed")
            return
        if candidate is None:
            logger.info("No acceptable map alignment; retrying")
            return
        with self._lock:
            if self._stopping or self._placement is not None or self._rotation_aborted:
                return
            self._candidate = candidate
            assert self._premap is not None
            self.alignment_preview.publish(self._premap.transform(candidate))
        logger.warning(
            "Alignment candidate ready. Compare alignment_preview with alignment_scan "
            "in Rerun, then use confirm_alignment() or reject_alignment() in dimos shell."
        )

    def _on_pgo_stream_error(self, error: Exception) -> None:
        with self._lock:
            self._pgo_error = str(error)
            self.pgo_stop.publish(Bool(True))
            self.cmd_vel.publish(Twist())
        logger.error("PGO lidar processing stopped; restart required", error=str(error))

    def _on_lidar(self, cloud: PointCloud2) -> None:
        if cloud.frame_id != "world":
            logger.error("Persistent Go2 mapping requires world-frame lidar", frame=cloud.frame_id)
            return
        with self._lock:
            if self._stopping:
                return
            if self._pgo_error is not None:
                self.cmd_vel.publish(Twist())
                return
            if self._placement is None:
                self._last_lidar_rx = time.monotonic()
                if self.config.startup_rotation or self.config.manual_capture:
                    if self._rotation_aborted:
                        return
                    if self._capture_grid is not None:
                        if (
                            self._manual_capture_active
                            or self._rotation_started is not None
                            or self._capture_frames == 0
                        ):
                            self._capture_grid.add_frame(cloud)
                            self._capture_frames += 1
                            if self._capture_frames % self.config.emit_every == 0:
                                self.alignment_scan.publish(
                                    self._capture_grid.get_global_pointcloud2()
                                )
                    elif self._capture_cloud is not None:
                        self._scans.on_next(self._capture_cloud)
                    return
                self.alignment_scan.publish(cloud)
                self._scans.on_next(cloud)
                return
            assert self._grid is not None
            aligned = cloud.transform(self._placement)
            fusion = self.fusion_status()
            if fusion["reason"] != self._fusion_last_reason:
                self._fusion_last_reason = fusion["reason"]
                logger.info("Map fusion state changed", **fusion)
            if not fusion["fusion_enabled"]:
                self._fusion_skipped += 1
                if self._pgo is not None:
                    self.pgo_raw_lidar.publish(aligned)
                    if self._pgo_graph is not None:
                        aligned = aligned.transform(self._pgo_graph.world_correction(cloud.ts))
                self.lidar.publish(aligned)
                return
            if self._pgo is not None:
                self.pgo_raw_lidar.publish(aligned)
            if self._pgo is None:
                self._grid.add_frame(aligned)
            else:
                pose = None
                if self._latest_odom is not None:
                    pose = (
                        self._placement + Transform.from_pose("base_link", self._latest_odom)
                    ).to_pose(ts=self._latest_odom.ts)
                try:
                    rebuilt = self._pgo.add(aligned, pose)
                    graph = self._pgo.graph()
                    revision = None
                    if rebuilt:
                        self._pgo_paused = True
                        self.cmd_vel.publish(Twist())
                        assert self._pgo_navigation is not None
                        revision = self._pgo_navigation.pause_for_pgo()
                    if graph.keyframes:
                        assert self._pgo_memory is not None
                        if (
                            rebuilt
                            or self._pgo_graph is None
                            or len(graph.keyframes) != len(self._pgo_graph.keyframes)
                        ):
                            self._pgo_memory.update_pgo_graph(
                                graph, self._pgo_session, self._current_map()
                            )
                        self._pgo_graph = graph
                        aligned = aligned.transform(graph.world_correction(cloud.ts))
                    if rebuilt:
                        self.cmd_vel.publish(Twist())
                        logger.info("PGO loop corrected map and tags; refreshing navigation target")
                        if self._latest_odom is not None:
                            self._on_odom(self._latest_odom)
                        if self._latest_tf is not None:
                            self._on_tf(self._latest_tf)
                    self._pgo_error = None
                except Exception as error:
                    self._pgo_error = str(error)
                    self.pgo_stop.publish(Bool(True))
                    self.cmd_vel.publish(Twist())
                    logger.exception("PGO synchronization failed; navigation and saving blocked")
                    raise
            self._frames += 1
            self.lidar.publish(aligned)
            if self._frames == 1 or self._frames % self.config.emit_every == 0:
                self.global_map.publish(self._current_map())
            if self._pgo is not None and rebuilt:
                try:
                    self.save_map()
                    assert self._pgo_navigation is not None
                    odom = None
                    if self._latest_odom is not None:
                        assert self._placement is not None
                        odom = self._correct_pose(
                            self._placement + Transform.from_pose("base_link", self._latest_odom),
                            self._latest_odom.ts,
                        ).to_pose(ts=self._latest_odom.ts)
                    assert revision is not None
                    self._pgo_navigation.resume_after_pgo(revision, self._current_map(), odom)
                    self._pgo_paused = False
                except Exception as error:
                    self._pgo_error = str(error)
                    self.pgo_stop.publish(Bool(True))
                    self.cmd_vel.publish(Twist())
                    logger.exception(
                        "PGO checkpoint or navigation refresh failed; navigation blocked"
                    )
                    raise

    def _current_map(self) -> PointCloud2:
        assert self._grid is not None
        if self._pgo is None:
            return self._grid.get_global_pointcloud2()
        live = self._pgo.global_map()
        result = (self._premap + live) if self._premap is not None else live
        result.ts = live.ts
        return result

    def _correct_pose(self, transform: Transform, timestamp: float) -> Transform:
        if self._pgo_graph is not None:
            transform = self._pgo_graph.world_correction(timestamp) + transform
        return transform

    def _on_odom(self, pose: PoseStamped) -> None:
        with self._lock:
            if self._stopping:
                return
            if self.config.auto_pause_fusion and not self._fusion_gate.on_pose(
                pose, time.monotonic()
            ):
                logger.error("Invalid odometry; permanent map fusion paused")
                return
            self._latest_odom = pose
            self._last_odom_rx = time.monotonic()
            if self._placement is not None:
                if pose.frame_id != "world":
                    logger.error("Persistent Go2 mapping requires world-frame odometry")
                    return
                aligned = self._placement + Transform.from_pose("base_link", pose)
                aligned = self._correct_pose(aligned, pose.ts)
                self.odom.publish(aligned.to_pose(ts=pose.ts))

    def _on_tf(self, message: TFMessage) -> None:
        with self._lock:
            if self._stopping:
                return
            self._latest_tf = message
            if self._placement is None:
                return
            transforms = []
            raw_transforms = []
            for transform in message.transforms:
                if transform.frame_id == "world":
                    aligned = self._placement + transform
                    aligned.ts = transform.ts
                    raw_transforms.append(aligned)
                    aligned = self._correct_pose(aligned, transform.ts)
                    aligned.ts = transform.ts
                    transforms.append(aligned)
                else:
                    transforms.append(transform)
                    raw_transforms.append(transform)
            if self._pgo is not None:
                self.pgo_raw_tf.publish(TFMessage(*raw_transforms))
            self.tf.publish(TFMessage(*transforms))

    @rpc
    def pause_fusion(self) -> str:
        """Pause permanent map updates, not sensors or robot motion."""
        with self._lock:
            if self._stopping:
                raise RuntimeError("Map module is stopping")
            self._fusion_manual_paused = True
        logger.info("Human paused permanent map fusion")
        return "Map fusion paused. Live sensors continue; this does not stop the robot or save."

    @rpc
    def resume_fusion(self) -> str:
        """Release the manual pause; automatic stationary gating still applies."""
        with self._lock:
            if self._stopping:
                raise RuntimeError("Map module is stopping")
            self._fusion_manual_paused = False
            status = self.fusion_status()
        logger.info("Human released permanent map fusion pause", **status)
        return (
            f"Manual pause released. {status['reason']}. Automatic gating still applies if enabled."
        )

    @rpc
    def fusion_status(self) -> dict[str, Any]:
        """Report permanent map fusion independently of sensor forwarding and navigation."""
        with self._lock:
            now = time.monotonic()
            enabled = True
            reason = "Automatic gate disabled"
            if self.config.auto_pause_fusion:
                enabled = self._fusion_gate.permits_fusion(now)
                reason = self._fusion_gate.reason
                if self._frames == 0 and self._fusion_gate.fresh_pose(now):
                    enabled, reason = True, "Accepting initial aligned map seed"
            if self._fusion_manual_paused:
                enabled, reason = False, "Manual pause"
            if self._placement is None:
                enabled, reason = False, "Waiting for alignment; startup capture is unaffected"
            if self._stopping or self._pgo_error is not None or self._pgo_paused:
                enabled, reason = False, "Map stopping or PGO blocked"
            return {
                "auto_enabled": self.config.auto_pause_fusion,
                "manual_paused": self._fusion_manual_paused,
                "motion_state": self._fusion_gate.state,
                "fusion_enabled": enabled,
                "reason": reason,
                "accepted_frames": self._frames,
                "skipped_frames": self._fusion_skipped,
                "speed_m_s": self._fusion_gate.speed,
                "rotation_deg_s": self._fusion_gate.rotation_deg,
            }

    @rpc
    def navigation_ready(self) -> bool:
        """Whether the map placement has been approved (or this is a new map)."""
        with self._lock:
            return not self._stopping and self._placement is not None and self._pgo_error is None

    @rpc
    def alignment_status(self) -> str:
        """Return readiness and the candidate transform; never approve an alignment."""
        with self._lock:
            if self._pgo_error is not None:
                return f"Blocked: PGO synchronization failed: {self._pgo_error}"
            if self._placement is not None:
                return "Ready: sensors and navigation use the saved world frame."
            if self._rotation_aborted:
                return "Blocked: startup capture was cancelled or failed; restart to retry."
            if self._manual_capture_active:
                assert self._capture_grid is not None
                return (
                    f"Blocked: manual capture has {len(self._capture_grid)} points "
                    f"from {self._capture_frames} scans. Stop the robot, then call "
                    "finish_startup_capture()."
                )
            if self._rotation_armed or self._rotation_started is not None:
                return (
                    "Blocked: startup rotation capture is waiting for sensors or collecting scans."
                )
            if self._candidate is None:
                return "Blocked: waiting for an alignment candidate."
            return f"Blocked: candidate {self._candidate}; inspect Rerun before confirming."

    @rpc
    def confirm_alignment(self) -> str:
        """Human-only approval through dimos shell; deliberately not an agent skill."""
        with self._lock:
            if self._placement is not None:
                return "Map alignment is already active."
            if self._candidate is None:
                raise RuntimeError("No alignment candidate is available")
            empty = PointCloud2.from_numpy(
                np.empty((0, 3)), frame_id="world", timestamp=time.time()
            )
            self.alignment_preview.publish(empty)
            self.alignment_scan.publish(empty)
            self._placement = Transform.from_matrix(
                self._candidate.inverse().to_matrix(),
                frame_id="world",
                child_frame_id="world",
            )
            if self._latest_tf is not None:
                self._on_tf(self._latest_tf)
            if self._latest_odom is not None:
                self._on_odom(self._latest_odom)
            assert self._grid is not None
            self.global_map.publish(self._grid.get_global_pointcloud2())
        logger.info("Human confirmed map alignment; navigation and map updates enabled")
        return "Confirmed. Navigation and new observations now use the saved map coordinates."

    @rpc
    def reject_alignment(self) -> str:
        """Discard an unapproved candidate and resume matching."""
        with self._lock:
            if self._placement is not None:
                raise RuntimeError("Cannot reject an active alignment; stop and restart instead")
            self.alignment_preview.publish(
                PointCloud2.from_numpy(np.empty((0, 3)), frame_id="world", timestamp=time.time())
            )
            self._candidate = None
        logger.info("Human rejected map alignment; matching will retry")
        return "Rejected. Waiting for another candidate."

    @rpc
    def save_map(self) -> str:
        """Atomically save the updated cloud without changing its coordinate system."""
        with self._lock:
            if self._pgo_error is not None:
                raise RuntimeError(
                    f"Cannot save after PGO synchronization failure: {self._pgo_error}"
                )
            if self._placement is None or self._frames == 0 or self._grid is None:
                raise RuntimeError("Cannot save before alignment and at least one accepted scan")
            assert self.config.map_file is not None
            path = Path(self.config.map_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            if self._pgo is not None:
                self._pgo.flush()
            data = self._current_map().lcm_encode()
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
                temporary = Path(file.name)
                try:
                    file.write(data)
                    file.flush()
                    os.fsync(file.fileno())
                except OSError:
                    temporary.unlink(missing_ok=True)
                    raise
            try:
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        logger.info("Saved persistent Go2 map", path=str(path))
        return f"Saved map to {path}"

    def _autosave(self) -> None:
        with self._lock:
            if self._placement is None or self._frames == 0 or self._grid is None:
                return
            if self._pgo_error is not None:
                logger.error("Map not saved: PGO synchronization failed", error=self._pgo_error)
                return
            try:
                self.save_map()
            except OSError:
                logger.exception("Could not save persistent Go2 map")

    @rpc
    def stop(self) -> None:
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
        try:
            with self._lock:
                if self._rotation_armed or self._rotation_started is not None:
                    self._cancel_rotation("Module shutdown stopped startup rotation")
        finally:
            try:
                super().stop()
                self._autosave()
            finally:
                with self._lock:
                    if self._grid is not None:
                        self._grid.dispose()
                        self._grid = None
                    if self._pgo is not None:
                        self._pgo.dispose()
                        self._pgo = None
                    if self._capture_grid is not None:
                        self._capture_grid.dispose()
                        self._capture_grid = None
                self._scans.dispose()


class PersistentGo2PlannerConfig(ReplanningAStarPlannerConfig):
    navigation_speed_limit: float | None = Field(default=None, ge=0.1, le=0.55, allow_inf_nan=False)
    nearby_arrival_distance: float = Field(default=1.0, ge=0.3, le=3.0, allow_inf_nan=False)
    visual_arrival_enabled: bool = False


class PersistentGo2Planner(ReplanningAStarPlanner):
    """Refuse navigation goals before the saved coordinate system is approved."""

    config: PersistentGo2PlannerConfig
    _map_session: PersistentGo2Map
    _spatial_memory: SpatialMemorySpec
    _costmapper: CostMapper
    _tag_view: TagViewSpec

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._goal_lock = RLock()
        self._speed_lock = RLock()
        self._navigation_speed_limit = self.config.navigation_speed_limit
        self._goal_revision = 0
        self._active_goal: PoseStamped | None = None
        self._active_tag: str | None = None
        self._pgo_paused = False
        self._costmap_ts_floor: float | None = None
        self._arrival_radius: float | None = None
        self._visual_required = False
        self._nearby_arrived: bool | None = None
        self._searching = False
        self._search_stop = Event()
        self._search_thread: Thread | None = None

    @rpc
    def navigation_speed_status(self) -> dict[str, Any]:
        with self._speed_lock:
            return {
                "enabled": self._navigation_speed_limit is not None,
                "speed_mps": self._navigation_speed_limit,
            }

    @rpc
    def set_navigation_speed(self, speed_mps: float) -> dict[str, Any]:
        if (
            isinstance(speed_mps, bool)
            or not isinstance(speed_mps, (int, float))
            or not math.isfinite(speed_mps)
            or not 0.1 <= speed_mps <= 0.55
        ):
            raise ValueError("Navigation speed must be between 0.10 and 0.55 m/s")
        with self._speed_lock:
            if self._navigation_speed_limit is None:
                raise ValueError("Live navigation speed is not enabled in this blueprint")
            self._navigation_speed_limit = float(speed_mps)
        logger.info("Updated live navigation speed limit", speed_mps=speed_mps)
        return self.navigation_speed_status()

    def _publish_navigation_velocity(self, velocity: Twist) -> None:
        with self._speed_lock:
            limit = self._navigation_speed_limit
            magnitude = math.sqrt(
                velocity.linear.x**2 + velocity.linear.y**2 + velocity.linear.z**2
            )
            if limit is not None and magnitude > limit:
                velocity = Twist(
                    linear=Vector3(
                        velocity.linear.x * limit / magnitude,
                        velocity.linear.y * limit / magnitude,
                        velocity.linear.z * limit / magnitude,
                    ),
                    angular=velocity.angular,
                )
            super()._publish_navigation_velocity(velocity)

    @rpc
    def start(self) -> None:
        super().start()
        self.register_disposable(self._planner.goal_reached.subscribe(self._on_goal_finished))

    @rpc
    def stop(self) -> None:
        super().stop()
        thread = self._search_thread
        if thread is not None and thread is not current_thread() and thread.is_alive():
            thread.join(DEFAULT_THREAD_JOIN_TIMEOUT)
            if thread.is_alive():
                logger.warning("Tag matching RPC still finishing; search motion is cancelled")

    def _on_goal_finished(self, result: Bool) -> None:
        with self._goal_lock:
            if not self._pgo_paused and not self._searching:
                if result.data and self._active_goal is not None:
                    if self._arrival_radius is None:
                        self.navigation_state.publish(String("Arrived at target"))
                    else:
                        pose = self._planner._current_odom
                        goal = self._active_goal
                        if (
                            pose is not None
                            and math.hypot(
                                goal.position.x - pose.position.x,
                                goal.position.y - pose.position.y,
                            )
                            <= self._arrival_radius + 1e-8
                        ):
                            if self._visual_required:
                                self._start_visual_search()
                                return
                            self.navigation_state.publish(String("Arrived within nearby threshold"))
                            self._nearby_arrived = True
                            self.goal_reached.publish(Bool(True))
                        else:
                            logger.warning("Planner stopped outside nearby arrival threshold")
                            self.navigation_state.publish(
                                String(
                                    "Navigation stopped outside nearby threshold; target may be unreachable"
                                )
                            )
                            self.goal_reached.publish(Bool(False))
                elif not result.data and self._arrival_radius is not None:
                    self._nearby_arrived = False
                    self.goal_reached.publish(Bool(False))
                self._active_goal = None
                self._active_tag = None
                self._arrival_radius = None

    def _publish_goal_result(self, result: Bool) -> None:
        with self._goal_lock:
            if self._arrival_radius is None and not self._pgo_paused and not self._searching:
                super()._publish_goal_result(result)

    @rpc
    def get_state(self) -> NavigationState:
        with self._goal_lock:
            return NavigationState.RECOVERY if self._searching else super().get_state()

    @rpc
    def is_goal_reached(self) -> bool:
        with self._goal_lock:
            if self._nearby_arrived is not None:
                return self._nearby_arrived
            return super().is_goal_reached()

    def _handle_odom(self, pose: PoseStamped) -> None:
        with self._goal_lock:
            self._planner.handle_odom(pose)
            if not self._pgo_paused and not self._searching:
                self._finish_nearby_goal(pose)

    def _finish_nearby_goal(self, pose: PoseStamped) -> bool:
        if self._active_goal is None or self._arrival_radius is None:
            return False
        goal = self._active_goal
        distance = math.hypot(goal.position.x - pose.position.x, goal.position.y - pose.position.y)
        if distance <= self._arrival_radius + 1e-8:
            if self._visual_required:
                self._start_visual_search()
                return True
            self._active_goal = None
            self._active_tag = None
            self._arrival_radius = None
            self._goal_revision += 1
            self._nearby_arrived = True
            self._planner.cancel_goal(arrived=True)
            self.nav_cmd_vel.publish(Twist())
            self.navigation_state.publish(String("Arrived within nearby threshold"))
            logger.info("Nearby navigation reached target", distance_m=distance)
            return True
        return False

    def _handle_global_costmap(self, grid: OccupancyGrid) -> None:
        with self._goal_lock:
            if self._pgo_paused:
                return
            if self._costmap_ts_floor is not None and grid.ts < self._costmap_ts_floor:
                return
            super()._handle_global_costmap(grid)

    def _handle_goal_request(self, goal: PoseStamped) -> bool:
        return self._accept_goal(goal, None)

    def _accept_goal(
        self, goal: PoseStamped, location_id: str | None, arrival_radius: float | None = None
    ) -> bool:
        with self._goal_lock:
            if self._pgo_paused:
                self.cancel_goal()
                logger.warning("Navigation refused: PGO synchronization is in progress")
                return False
        if not self._map_session.navigation_ready():
            logger.warning("Navigation refused: saved map alignment is not confirmed")
            return False
        with self._goal_lock:
            if self._pgo_paused:
                return False
            self._stop_visual_search()
            self._goal_revision += 1
            self._active_goal = copy.deepcopy(goal)
            self._active_tag = location_id
            self._arrival_radius = arrival_radius
            self._nearby_arrived = False if arrival_radius is not None else None
            self._visual_required = (
                arrival_radius is not None and self.config.visual_arrival_enabled
            )
            if self._planner._current_odom is not None and self._finish_nearby_goal(
                self._planner._current_odom
            ):
                return True
            return super()._handle_goal_request(goal)

    @rpc
    def set_tagged_goal(self, location_id: str, goal: PoseStamped) -> bool:
        return self._accept_goal(goal, location_id)

    @rpc
    def nearby_navigation_status(self) -> dict[str, float]:
        with self._goal_lock:
            return {"distance_m": self.config.nearby_arrival_distance}

    @rpc
    def set_nearby_arrival_distance(self, distance_m: float) -> dict[str, float]:
        if not math.isfinite(distance_m) or not 0.3 <= distance_m <= 3.0:
            raise ValueError("Nearby arrival distance must be between 0.3 and 3.0 meters")
        with self._goal_lock:
            self.config.nearby_arrival_distance = distance_m
            return self.nearby_navigation_status()

    @rpc
    def visual_arrival_status(self) -> dict[str, Any]:
        with self._goal_lock:
            return {"enabled": self.config.visual_arrival_enabled, "searching": self._searching}

    @rpc
    def configure_visual_arrival(self, enabled: bool) -> dict[str, Any]:
        with self._goal_lock:
            self.config.visual_arrival_enabled = enabled
            if not enabled:
                self._visual_required = False
            if not enabled and self._searching:
                self.cancel_goal()
                self.navigation_state.publish(String("Visual search cancelled by toggle"))
            return self.visual_arrival_status()

    def _stop_visual_search(self) -> None:
        self._search_stop.set()
        if self._searching:
            self.nav_cmd_vel.publish(Twist())
        self._searching = False

    def _start_visual_search(self) -> None:
        if self._searching:
            return
        if self._active_tag is None:
            raise RuntimeError("Visual search requires a saved tag ID")
        self._searching = True
        self._planner.cancel_goal()
        self.nav_cmd_vel.publish(Twist())
        self._search_stop = Event()
        self.navigation_state.publish(String("Nearby threshold reached; searching for tag image"))
        self._search_thread = Thread(
            target=self._search_tag_view,
            args=(self._goal_revision, self._active_tag, self._search_stop),
            name="Go2TagViewSearch",
            daemon=True,
        )
        self._search_thread.start()

    def _search_tag_view(self, revision: int, location_id: str, stop: Event) -> None:
        deadline = time.monotonic() + 20.0
        last_image_ts = float("-inf")
        try:
            while not stop.is_set():
                # The robot is stationary during slow RPC/image matching.
                result = self._tag_view.verify_tag_view(location_id)
                with self._goal_lock:
                    if stop.is_set() or revision != self._goal_revision or self._pgo_paused:
                        return
                    if time.monotonic() >= deadline:
                        self._finish_visual_search(
                            False, "Visual search timed out; no matching tag view"
                        )
                        return
                    timestamp = float(result["image_ts"])
                    if timestamp > last_image_ts and result["matched"] is True:
                        self._finish_visual_search(True, "Arrived: tag image matched")
                        return
                    last_image_ts = timestamp
                    self.nav_cmd_vel.publish(Twist(angular=Vector3(0, 0, 0.15)))
                # Short bounded turn pulses; stop commands and PGO interrupt immediately.
                if stop.wait(min(0.5, max(0, deadline - time.monotonic()))):
                    return
                with self._goal_lock:
                    if revision != self._goal_revision or stop.is_set():
                        return
                    self.nav_cmd_vel.publish(Twist())
        except Exception as error:
            logger.exception("Tag image search failed", location_id=location_id)
            with self._goal_lock:
                if revision == self._goal_revision and not stop.is_set():
                    self._finish_visual_search(False, f"Visual search failed: {error}")

    def _finish_visual_search(self, matched: bool, status: str) -> None:
        self._stop_visual_search()
        self._active_goal = None
        self._active_tag = None
        self._arrival_radius = None
        self._visual_required = False
        self._nearby_arrived = matched
        self._goal_revision += 1
        self.nav_cmd_vel.publish(Twist())
        self.navigation_state.publish(String(status))
        self.goal_reached.publish(Bool(matched))
        logger.info("Visual tag search stopped", matched=matched, status=status)

    @rpc
    def set_nearby_tagged_goal(self, location_id: str, goal: PoseStamped) -> bool:
        if goal.frame_id != "world" or not all(
            math.isfinite(value) for value in (goal.position.x, goal.position.y, goal.position.z)
        ):
            raise ValueError("Nearby navigation requires finite world coordinates")
        return self._accept_goal(
            goal, location_id, arrival_radius=self.config.nearby_arrival_distance
        )

    @rpc
    def cancel_goal(self) -> bool:
        with self._goal_lock:
            self._stop_visual_search()
            self._goal_revision += 1
            self._active_goal = None
            self._active_tag = None
            self._arrival_radius = None
            self._visual_required = False
            self._nearby_arrived = False
            return super().cancel_goal()

    @rpc
    def pause_for_pgo(self) -> int:
        with self._goal_lock:
            self._pgo_paused = True
            self._stop_visual_search()
            self._planner.cancel_goal()
            self.nav_cmd_vel.publish(Twist())
            self.navigation_state.publish(String("PGO correction: navigation paused"))
            return self._goal_revision

    @rpc
    def resume_after_pgo(
        self, revision: int, map_cloud: PointCloud2, odom: PoseStamped | None
    ) -> bool:
        with self._goal_lock:
            goal = copy.deepcopy(self._active_goal)
            location_id = self._active_tag
            if revision != self._goal_revision or goal is None:
                self._pgo_paused = False
                return False
        try:
            if odom is None:
                raise RuntimeError("Cannot resume navigation after PGO without aligned odometry")
            if location_id is not None:
                location = next(
                    (
                        item
                        for item in self._spatial_memory.get_robot_locations()
                        if item.location_id == location_id
                    ),
                    None,
                )
                if location is None:
                    raise RuntimeError(f"Navigation tag {location_id!r} no longer exists")
                if not all(
                    math.isfinite(value) for value in (*location.position, *location.rotation)
                ):
                    raise ValueError(f"Navigation tag {location_id!r} has invalid coordinates")
                goal.position = Vector3(location.position[0], location.position[1], odom.position.z)
                goal.orientation = Quaternion.from_euler(Vector3(*location.rotation))
            goal.frame_id = "world"
            goal.ts = odom.ts
            costmap = self._costmapper.calculate_navigation_costmap(map_cloud)
            with self._goal_lock:
                if revision != self._goal_revision or self._active_goal is None:
                    self._pgo_paused = False
                    return False
                self._planner.handle_global_costmap(costmap)
                self._costmap_ts_floor = map_cloud.ts
                self._planner.handle_odom(odom)
                self._active_goal = copy.deepcopy(goal)
                self._pgo_paused = False
                if self._finish_nearby_goal(odom):
                    return True
                super()._handle_goal_request(goal)
                self.navigation_state.publish(String("PGO correction: navigation replanned"))
                logger.info("Navigation resumed after PGO", tag_id=location_id, goal=str(goal))
                return True
        except Exception:
            self.cancel_goal()
            self._pgo_paused = False
            self.navigation_state.publish(String("PGO navigation refresh failed; stopped"))
            logger.exception("Could not resume navigation after PGO")
            raise
