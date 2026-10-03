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

import math
import os
from pathlib import Path
import tempfile
from threading import RLock
import time
from typing import Any

from dimos_lcm.std_msgs import Bool
import numpy as np
from reactivex import Subject, interval, operators as ops
from reactivex.disposable import Disposable

from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.core.stream import In, Out
from dimos.mapping.relocalization.lidar.module import window
from dimos.mapping.relocalization.lidar.relocalize import MID360, LidarRelocalizer, RelocalizeConfig
from dimos.mapping.voxels.grid import VoxelGrid
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Transform import Transform
from dimos.msgs.geometry_msgs.Twist import Twist
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.sensor_msgs.PointCloud2 import PointCloud2
from dimos.msgs.tf2_msgs.TFMessage import TFMessage
from dimos.navigation.go2.replanning_a_star.module import ReplanningAStarPlanner
from dimos.utils.logging_config import setup_logger
from dimos.utils.reactive import backpressure

logger = setup_logger()


class PersistentGo2MapConfig(ModuleConfig):
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
    global_map: Out[PointCloud2]
    alignment_preview: Out[PointCloud2]
    alignment_scan: Out[PointCloud2]

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

    def _on_lidar(self, cloud: PointCloud2) -> None:
        if cloud.frame_id != "world":
            logger.error("Persistent Go2 mapping requires world-frame lidar", frame=cloud.frame_id)
            return
        with self._lock:
            if self._stopping:
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
            self._grid.add_frame(aligned)
            self._frames += 1
            self.lidar.publish(aligned)
            if self._frames == 1 or self._frames % self.config.emit_every == 0:
                self.global_map.publish(self._grid.get_global_pointcloud2())

    def _on_odom(self, pose: PoseStamped) -> None:
        with self._lock:
            if self._stopping:
                return
            self._latest_odom = pose
            self._last_odom_rx = time.monotonic()
            if self._placement is not None:
                if pose.frame_id != "world":
                    logger.error("Persistent Go2 mapping requires world-frame odometry")
                    return
                aligned = self._placement + Transform.from_pose("base_link", pose)
                self.odom.publish(aligned.to_pose(ts=pose.ts))

    def _on_tf(self, message: TFMessage) -> None:
        with self._lock:
            if self._stopping:
                return
            self._latest_tf = message
            if self._placement is None:
                return
            transforms = []
            for transform in message.transforms:
                if transform.frame_id == "world":
                    aligned = self._placement + transform
                    aligned.ts = transform.ts
                    transforms.append(aligned)
                else:
                    transforms.append(transform)
            self.tf.publish(TFMessage(*transforms))

    @rpc
    def navigation_ready(self) -> bool:
        """Whether the map placement has been approved (or this is a new map)."""
        with self._lock:
            return not self._stopping and self._placement is not None

    @rpc
    def alignment_status(self) -> str:
        """Return readiness and the candidate transform; never approve an alignment."""
        with self._lock:
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
            if self._placement is None or self._frames == 0 or self._grid is None:
                raise RuntimeError("Cannot save before alignment and at least one accepted scan")
            assert self.config.map_file is not None
            path = Path(self.config.map_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = self._grid.get_global_pointcloud2().lcm_encode()
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
                    if self._capture_grid is not None:
                        self._capture_grid.dispose()
                        self._capture_grid = None
                self._scans.dispose()


class PersistentGo2Planner(ReplanningAStarPlanner):
    """Refuse navigation goals before the saved coordinate system is approved."""

    _map_session: PersistentGo2Map

    def _handle_goal_request(self, goal: PoseStamped) -> bool:
        if not self._map_session.navigation_ready():
            logger.warning("Navigation refused: saved map alignment is not confirmed")
            return False
        return super()._handle_goal_request(goal)
