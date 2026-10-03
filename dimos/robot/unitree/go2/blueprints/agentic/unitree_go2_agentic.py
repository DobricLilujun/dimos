#!/usr/bin/env python3
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

from dimos.agents.mcp.mcp_client import McpClient
from dimos.agents.mcp.mcp_server import McpServer
from dimos.agents.skills.person_recognition import NamedPersonRecognizerSkillContainer
from dimos.agents.skills.scene_graph_server import SceneGraphServerModule
from dimos.core.coordination.blueprints import autoconnect
from dimos.robot.unitree.go2.blueprints.agentic._common_agentic import _common_agentic
from dimos.robot.unitree.go2.blueprints.smart.unitree_go2_spatial import unitree_go2_spatial

# _common_agentic = autoconnect(...)
unitree_go2_agentic = autoconnect(
    unitree_go2_spatial,
    # MCP client: an autonomous agent that fetches tools (skills) from the MCP server and
    # executes them. It receives user input via WebInput's input stream.
    McpServer.blueprint(),
    McpClient.blueprint(),
    _common_agentic,
    # --- Three new features (all inert by default; opt-in via CLI flags) ---
    # (1) Scene map / scene graph: SpatialMemory (in unitree_go2_spatial) already
    #     builds a native real-time scene map. Load a previously-generated one with
    #     `--spatial-memory.scene-map-dir <path>` (see SpatialConfig.scene_map_dir).
    # (2) Recognize named people in the live camera and say "I found <name>"
    #     through the Go2 Pro speaker:
    #     `--named-person-recognizer-skill-container.gallery-dir <path>`
    #     (path has one sub-folder per person, e.g. <path>/alice/*.jpg).
    NamedPersonRecognizerSkillContainer().blueprint(),
    # (3) A small web "dialog" server that reports the scene graph's contents:
    #     `--scene-graph-server.port 5556` -> http://127.0.0.1:5556/scene_graph
    #     (interactive query: /query?q=<text>).
    SceneGraphServerModule().blueprint(),
)
