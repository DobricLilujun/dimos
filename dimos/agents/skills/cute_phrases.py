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

"""Playful puppy-tone English one-liners spoken on mode (skill) transitions.

Each entry maps a mode skill name to the line said when the mode is *entered*
and when it is *exited*. A mode is entered the first time its background
tool-stream becomes active and exited when the stream reports itself stopped.
Unknown skills are simply not announced (the table is the whitelist).
"""

from __future__ import annotations

MODE_PHRASES: dict[str, tuple[str, str]] = {
    # Exploration modes
    "begin_demo_exploration": (
        "Woof! Time to go explore — let's find all the new stuff!",
        "Exploring's done for now — back to rest, woof!",
    ),
    "begin_exploration": (
        "Woof! Time to go explore — let's find all the new stuff!",
        "Exploring's done for now — back to rest, woof!",
    ),
    "end_demo_exploration": (
        "",
        "Exploring's done for now — back to rest, woof!",
    ),
    "end_exploration": (
        "",
        "Exploring's done for now — back to rest, woof!",
    ),
    # Follow / person modes
    "follow_person": (
        "I'm following you now — let's go go!",
        "Follow's over — see you later!",
    ),
    "follow_person_with_planner": (
        "I'm following you now — let's go go!",
        "Follow's over — see you later!",
    ),
    "end_person_follow": (
        "",
        "Follow's over — see you later!",
    ),
    "stop_person_follow": (
        "",
        "Follow's over — see you later!",
    ),
    # Navigation
    "navigate_with_text": (
        "On it! Heading there now.",
        "",
    ),
    "navigate_to_memory_tag": (
        "On my way — hold tight!",
        "",
    ),
    "navigate_near_memory_tag": (
        "Coming over there, woof!",
        "",
    ),
    "return_to_starting_location": (
        "Heading back to where we started!",
        "",
    ),
    "stop_navigation": (
        "",
        "Okay, stopping now!",
    ),
    # Notable actions
    "tag_object": (
        "Woof! Tagged it — that's my territory now!",
        "",
    ),
    "tag_location": (
        "Tagged this spot, woof!",
        "",
    ),
}

# A neutral fallback so any mode the agent picks still gets a friendly line.
DEFAULT_ENTER = "Okay! Getting to it now, woof!"
DEFAULT_EXIT = "All done for now!"


def enter_phrase(tool_name: str) -> str | None:
    """Return the 'entered' line for a mode skill, or None if not a mode."""
    entry = MODE_PHRASES.get(tool_name)
    if entry is None:
        return None
    return entry[0] or None


def exit_phrase(tool_name: str) -> str | None:
    """Return the 'exited' line for a mode skill, or None if not a mode."""
    entry = MODE_PHRASES.get(tool_name)
    if entry is None:
        return None
    return entry[1] or None


def is_mode_tool(tool_name: str) -> bool:
    return tool_name in MODE_PHRASES


def phrase_for_transition(
    tool_name: str, *, entered: bool, default: bool = True
) -> str | None:
    """The line to speak for a transition; falls back to a neutral line.

    Falls back to ``DEFAULT_ENTER``/``DEFAULT_EXIT`` only when ``default`` is
    True *and* the skill is not in the whitelist, so unknown tools stay silent
    unless the caller opts in.
    """
    specific = enter_phrase(tool_name) if entered else exit_phrase(tool_name)
    if specific is not None:
        return specific
    if default:
        return DEFAULT_ENTER if entered else DEFAULT_EXIT
    return None
