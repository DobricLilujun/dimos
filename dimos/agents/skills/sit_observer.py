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

"""Sit the Go2 to look up — a better upward view for explore and tagging.

The Go2's camera and lidar sit on top of the body, so when it **sits** (or
bends its hind legs and crouches) the top sensors pitch further upward and can
see higher and overhead objects. This is the same one-tap action the Unitree
Go2 app ships by default. A sit is worth it right before the robot tags a tall
object, inspects an elevated frontier, or otherwise needs to see above eye
level — it can be triggered manually (agent / web console) or automatically
(the agent decides from the scene, or exploration asks for it), and the robot
stands back up on its own afterwards.
"""

from __future__ import annotations

import time
from typing import Any

from dimos.agents.annotation import skill
from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.robot.unitree.go2.connection_spec import GO2ConnectionSpec
from dimos.utils.logging_config import setup_logger

logger = setup_logger()


class SitObserverConfig(ModuleConfig):
    # Dwell (seconds) the robot stays seated during ``sit_to_observe`` before it
    # rises and recovers to a walking posture on its own.
    sit_dwell_seconds: float = 3.0
    # Run RecoveryStand after rising so the robot is walking-ready again.
    recovery_stand_after_sit: bool = True
    # When true, a caller (e.g. exploration) may ask the observer to sit on its
    # own; when false only explicit skill / console calls sit the robot.
    auto_sit_observe: bool = False


class SitObserver(Module):
    """Sit / crouch the Go2 to look up, then rise and recover on its own."""

    config: SitObserverConfig
    _connection: GO2ConnectionSpec
    # Optional: a speak(text, blocking) callable (e.g. SpeakSkill) for a cute
    # one-liner. Injected via remappings; a None value just silences the line.
    _speak: Any = None
    _speak_fn: Any = None
    _sitting: bool = False

    @rpc
    def start(self) -> None:
        super().start()
        self._speak_fn = self._speak

    @property
    def is_sitting(self) -> bool:
        return self._sitting

    def _announce(self, text: str) -> None:
        if self._speak_fn is None:
            return
        try:
            self._speak_fn(text, False)
        except Exception:
            logger.exception("SitObserver failed to speak: %s", text)

    @skill
    def sit(self) -> str:
        """Sit the robot down (crouch on its haunches) so the top camera and lidar look more upward.

        Use when you need to see higher or overhead objects before tagging or
        inspecting an elevated target. The robot stays seated until
        rise_sit() or sit_to_observe() is called.

        Returns:
            Confirmation message, or an error if the robot could not sit.
        """
        ok = self._connection.sit()
        if not ok:
            return "Could not sit the robot."
        self._sitting = True
        self._announce("Whoa — sitting up to look around, woof!")
        return "Sitting now; look up for a wider view."

    @skill
    def rise_sit(self) -> str:
        """Rise back to a standing posture from sitting.

        Returns:
            Confirmation message.
        """
        ok = self._connection.rise_sit()
        if not ok:
            return "Could not rise from sitting."
        self._sitting = False
        if self.config.recovery_stand_after_sit:
            self._connection.recovery_stand()
        self._announce("All done — standing back up!")
        return "Risen and standing."

    @skill
    def sit_to_observe(self, seconds: float = 3.0) -> str:
        """Sit, hold for a moment to look up and capture the view, then rise on its own.

        Args:
            seconds: How long to stay seated (default 3.0). Use this before
                tagging a tall object or inspecting an elevated frontier so the
                top-mounted camera and lidar can see above eye level.

        Returns:
            Confirmation message describing the sit/observe/rise sequence.
        """
        if seconds <= 0:
            return "seconds must be positive."
        if not self._connection.sit():
            return "Could not sit the robot."
        self._sitting = True
        self._announce("Whoa — sitting up to look around, woof!")
        try:
            time.sleep(seconds)
        finally:
            self._connection.rise_sit()
            self._sitting = False
            if self.config.recovery_stand_after_sit:
                self._connection.recovery_stand()
        self._announce("All done — standing back up!")
        return f"Sated the upward view for {seconds:g}s, then rose and stood."
