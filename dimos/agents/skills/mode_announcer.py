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

"""Speak a playful one-liner when the agent enters or exits a mode (skill).

A "mode" here is a skill the agent calls that changes what the robot is doing
(explore, follow, navigate, ...). This module listens on the shared tool-stream
topic (the same channel ``McpClient`` uses) and speaks a cute English line the
first time a mode's background stream becomes active, and speaks the exit line
when that stream reports itself stopped. The line is chosen by
:mod:`cute_phrases`; repeated identical transitions are de-duplicated so a mode
is not announced twice in a row.
"""

from __future__ import annotations

from typing import Any

from dimos.agents.mcp import tool_stream
from dimos.agents.skills.cute_phrases import (
    enter_phrase,
    exit_phrase,
    is_mode_tool,
)
from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.utils.logging_config import setup_logger

logger = setup_logger()


class ModeAnnouncerConfig(ModuleConfig):
    enabled: bool = True
    # Speak a neutral line for any mode not in the cute-phrase whitelist.
    announce_unknown: bool = False


class ModeAnnouncer(Module):
    """Speak on mode (skill) enter/exit transitions."""

    config: ModeAnnouncerConfig
    # A callable that speaks text; injected from SpeakSkill via remappings so the
    # announcer never has its own TTS and reuses the robot's voice.
    _speak: Any  # callable((str, bool)) -> str, or a SpeakSkillSpec

    _cleanup: Any = None
    _announced: set[str] = set()  # modes already announced as entered
    _speak_fn: Any = None

    @rpc
    def start(self) -> None:
        super().start()
        if not self.config.enabled:
            logger.info("Mode announcer disabled")
            return
        self._speak_fn = self._speak
        self._cleanup = tool_stream.subscribe(self._on_message)

    @rpc
    def stop(self) -> None:
        if self._cleanup is not None:
            self._cleanup()
            self._cleanup = None
        super().stop()

    def _on_message(self, msg: dict[str, Any]) -> None:
        method: str | None = msg.get("method")
        params = msg.get("params") or {}
        tool_name: str | None = None
        if method == tool_stream.NOTIFICATIONS_PROGRESS_METHOD:
            tool_name = (params.get("_meta") or {}).get("tool_name")
        elif method == tool_stream.NOTIFICATIONS_MESSAGE_METHOD:
            tool_name = params.get("logger")

        if method == tool_stream.TOOL_STREAM_STOPPED_METHOD:
            # A stopped frame carries only the tool name; announce exit.
            tool_name = params.get("tool_name")

        if method is None or tool_name is None:
            return
        self._handle_transition(method, tool_name)

    def _handle_transition(self, method: str, tool_name: str) -> None:
        if method == tool_stream.TOOL_STREAM_STOPPED_METHOD:
            if tool_name in self._announced:
                self._announced.discard(tool_name)
                line = exit_phrase(tool_name)
                if line is not None:
                    self._announce(line)
            return

        # A progress/message frame means a background mode is running; announce
        # its entry once, then ignore further frames for that mode until it
        # stops, so the line is not repeated on every progress update.
        if tool_name in self._announced:
            return
        if not is_mode_tool(tool_name):
            return
        self._announced.add(tool_name)
        line = enter_phrase(tool_name)
        if line is None and self.config.announce_unknown:
            from dimos.agents.skills.cute_phrases import phrase_for_transition

            line = phrase_for_transition(tool_name, entered=True, default=True)
        if line is not None:
            self._announce(line)

    def _announce(self, line: str) -> None:
        if self._speak_fn is None:
            return
        try:
            # Non-blocking so a slow TTS never blocks the tool-stream thread.
            self._speak_fn(line, False)
        except Exception:
            logger.exception("Mode announcer failed to speak: %s", line)
