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

"""A person that walks a closed loop in the MuJoCo office, as a moving goal target.

The MuJoCo scene has a mocap ``person`` body that follows ``Pose`` messages on
``/person_pose`` (see ``person_on_track.py``). This module publishes those poses
and, once ``start_delay_s`` has passed, the same positions as ``tracked_target``.
"""

import math
from threading import Event, Thread
import time
from typing import Any

from pydantic import Field

from dimos.constants import DEFAULT_THREAD_JOIN_TIMEOUT
from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.core.stream import Out
from dimos.core.transport import PubSubTransport
from dimos.core.transport_factory import make_transport
from dimos.msgs.geometry_msgs.Pose import Pose
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Quaternion import Quaternion
from dimos.msgs.geometry_msgs.Vector3 import Vector3

# Out and back along two legs of the track the e2e person tests already use
# (test_person_follow.py), starting near the robot's start at (-6.18, 0.96).
# Every segment is one the person is known to walk through; the closing
# segment retraces the second leg.
DEFAULT_TRACK: list[tuple[float, float]] = [
    (-3.35, -0.51),
    (-2.60, 1.28),
    (1.10, 0.745),
    (-2.60, 1.28),
]


class TrackWalker:
    """Walks a closed polyline at constant speed."""

    def __init__(self, track: list[tuple[float, float]], speed_mps: float) -> None:
        if len(track) < 2:
            raise ValueError("A track needs at least two waypoints")
        length = sum(
            math.dist(track[index], track[(index + 1) % len(track)]) for index in range(len(track))
        )
        if length <= 0.0:
            raise ValueError("A track must cover some distance")
        self._track = track
        self._speed_mps = speed_mps
        self._next = 1
        self._position = track[0]
        self._heading = 0.0
        self._face_next_waypoint()

    @property
    def position(self) -> tuple[float, float]:
        return self._position

    @property
    def heading(self) -> float:
        """Direction of travel in radians; unchanged while standing on a waypoint."""
        return self._heading

    def advance(self, dt: float) -> None:
        remaining = self._speed_mps * dt
        while remaining > 0.0:
            goal = self._track[self._next]
            gap = math.dist(self._position, goal)
            if gap <= remaining:
                self._position = goal
                remaining -= gap
                self._next = (self._next + 1) % len(self._track)
                continue
            fraction = remaining / gap
            self._position = (
                self._position[0] + (goal[0] - self._position[0]) * fraction,
                self._position[1] + (goal[1] - self._position[1]) * fraction,
            )
            remaining = 0.0
        self._face_next_waypoint()

    def _face_next_waypoint(self) -> None:
        dx = self._track[self._next][0] - self._position[0]
        dy = self._track[self._next][1] - self._position[1]
        if dx != 0.0 or dy != 0.0:
            self._heading = math.atan2(dy, dx)


class MujocoPersonTargetConfig(ModuleConfig):
    track: list[tuple[float, float]] = Field(default_factory=lambda: list(DEFAULT_TRACK))
    speed_mps: float = Field(default=0.25, gt=0.0, le=0.5, allow_inf_nan=False)
    rate_hz: float = Field(default=30.0, gt=0.0, le=100.0, allow_inf_nan=False)
    # The person stands at the first waypoint this long, so the robot can map
    # its surroundings before the first goal.
    start_delay_s: float = Field(default=10.0, ge=0.0, allow_inf_nan=False)


class MujocoPersonTarget(Module):
    config: MujocoPersonTargetConfig

    tracked_target: Out[PoseStamped]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._person_pose: PubSubTransport[Pose] = make_transport("/person_pose", Pose)

    @rpc
    def start(self) -> None:
        super().start()
        self._stop_event.clear()
        self._thread = Thread(target=self._run, name="MujocoPersonTarget", daemon=True)
        self._thread.start()

    @rpc
    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(DEFAULT_THREAD_JOIN_TIMEOUT)
            self._thread = None
        self._person_pose.stop()
        super().stop()

    def _run(self) -> None:
        walker = TrackWalker(self.config.track, self.config.speed_mps)
        period = 1.0 / self.config.rate_hz
        walking_from = time.monotonic() + self.config.start_delay_s
        last_tick = walking_from

        while not self._stop_event.is_set():
            now = time.monotonic()
            walking = now >= walking_from
            if walking:
                walker.advance(now - max(last_tick, walking_from))
            last_tick = now

            x, y = walker.position
            self._publish_person_pose(x, y, walker.heading)
            if walking:
                self.tracked_target.publish(
                    PoseStamped(
                        ts=time.time(),
                        frame_id="world",
                        position=Vector3(x, y, 0.0),
                        orientation=Quaternion.from_euler(Vector3(0.0, 0.0, walker.heading)),
                    )
                )

            self._stop_event.wait(period)

    def _publish_person_pose(self, x: float, y: float, heading: float) -> None:
        # The mesh faces backwards, hence the half turn (as in PersonTrackPublisher).
        yaw = heading + math.pi
        pose = Pose(
            position=[x, y, 0.0],
            orientation=[0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2)],
        )
        self._person_pose.broadcast(None, pose)
