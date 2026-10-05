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

"""The Go2 agentic *persistent* workflow with the SEDAN GROUP web console.

Same stack as :data:`unitree_go2_agentic_persistent`, plus a
:class:`~dimos.web.console.module.RobotConsoleModule` that serves a web
control deck + ChatGPT-style chat on ``:8090``. The console embeds the
existing Rerun web viewer by iframe (original Rerun content preserved) and
adds:

* button-driven control of the workflow operations in the
  ``unitree-go2-agentic-persistent`` usage tutorial (alignment, map capture,
  object/location tagging, memory query, navigation, stop),
* a chat that sends text to the agent (``/human_input``) and renders the
  agent's replies (``/agent``), the robot's feedback, and the internal tool
  input/output (``/agent`` tool messages + ``/tool_streams``).

The console module declares no ``In``/``Out`` ports, so it never competes with
the persistent stack's stream wiring; it only observes/publishes by channel
name through the shared transport.
"""

from dataclasses import replace

from dimos.agents.mcp.mcp_client import McpClient
from dimos.core.coordination.blueprints import autoconnect
from dimos.robot.unitree.go2.blueprints.agentic.unitree_go2_agentic_persistent import (
    unitree_go2_agentic_persistent,
)
from dimos.robot.unitree.go2.connection import GO2Connection
from dimos.visualization.rerun.bridge import RerunBridgeModule
from dimos.web.console.module import RobotConsoleModule
from dimos.web.console.prompts import CONSOLE_AGENT_PROMPT

unitree_go2_agentic_persistent_console = autoconnect(
    replace(
        unitree_go2_agentic_persistent,
        blueprints=tuple(
            replace(atom, kwargs={**atom.kwargs, "rerun_web": True})
            if atom.module is RerunBridgeModule
            else replace(
                atom,
                kwargs={
                    **atom.kwargs,
                    "system_prompt": CONSOLE_AGENT_PROMPT,
                },
            )
            if atom.module is McpClient
            else replace(atom, kwargs={**atom.kwargs, "puppy_enabled": True})
            if atom.module is GO2Connection
            else atom
            for atom in unitree_go2_agentic_persistent.blueprints
        ),
    ),
    RobotConsoleModule.blueprint(),
).global_config(n_workers=9)
