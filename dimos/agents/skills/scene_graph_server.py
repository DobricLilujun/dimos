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

"""A tiny web "dialog" server that reports the scene graph.

Reuses features that already exist in DimOS: the native scene map / scene graph
built by :class:`SpatialMemory` (queried through :class:`SpatialMemorySpec`) and
the robot's own web-server pattern (this mirrors ``WebInput``/``FastAPIServer``).

It exposes a very small HTTP surface so an operator can ask, from any browser or
``curl``, what the robot's scene graph currently contains:

* ``GET /``            -> a minimal HTML page that lists the scene graph.
* ``GET /scene_graph`` -> JSON of named locations + map stats.
* ``GET /query?q=...`` -> interactive: query the scene graph with a natural
  language string and return the best matching items (positions included).

Opt-in: when ``port`` is ``None`` (the default) the module is a no-op, so the
``unitree-go2-agentic`` blueprint is unchanged until
``--scene-graph-server.port <n>`` is passed.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from typing import Any
from urllib.parse import parse_qs, urlparse

from dimos.constants import DEFAULT_THREAD_JOIN_TIMEOUT
from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.perception.experimental.spatial_memory_spec import SpatialMemorySpec
from dimos.utils.logging_config import setup_logger

logger = setup_logger()


class SceneGraphServerConfig(ModuleConfig):
    # ``None`` disables the server (no-op) so the blueprint is unchanged by default.
    port: int | None = None
    host: str = "127.0.0.1"


class SceneGraphServerModule(Module):
    """Serve the robot's scene graph over a minimal HTTP dialog interface."""

    config: SceneGraphServerConfig
    _spatial_memory: SpatialMemorySpec | None = None  # injected by blueprint

    _server: ThreadingHTTPServer | None = None
    _thread: Thread | None = None

    @rpc
    def start(self) -> None:
        super().start()
        if self.config.port is None:
            logger.info("SceneGraphServer: no port configured; disabled (no-op).")
            return
        if self._spatial_memory is None:
            logger.warning("SceneGraphServer: no scene graph (SpatialMemory) wired; disabled.")
            return

        memory = self._spatial_memory
        host, port = self.config.host, self.config.port

        handler = _make_handler(memory)
        self._server = ThreadingHTTPServer((host, port), handler)
        self._thread = Thread(
            target=self._server.serve_forever, daemon=True, name="scene-graph-server"
        )
        self._thread.start()
        logger.info(f"SceneGraphServer: scene-graph dialog at http://{host}:{port}/scene_graph")

    @rpc
    def stop(self) -> None:
        if self._thread is not None:
            self._thread.join(timeout=DEFAULT_THREAD_JOIN_TIMEOUT)
            self._thread = None
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:  # pragma: no cover
                pass
            self._server = None
        super().stop()

    # -- data helpers (shared by the handler) ----------------------------------
    @staticmethod
    def _locations_payload(memory: SpatialMemorySpec) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for location in memory.get_robot_locations():
            position = getattr(location, "position", None)
            if position is not None:
                try:
                    position = list(position)  # RobotLocation stores a tuple of floats
                except TypeError:
                    position = [
                        getattr(position, "x", None),
                        getattr(position, "y", None),
                        getattr(position, "z", None),
                    ]
            payload.append({"name": getattr(location, "name", ""), "position": position})
        return payload

    @staticmethod
    def _scene_graph_payload(memory: SpatialMemorySpec) -> dict[str, Any]:
        return {
            "locations": SceneGraphServerModule._locations_payload(memory),
            "stats": memory.get_stats(),
        }

    @staticmethod
    def _query_payload(memory: SpatialMemorySpec, query: str) -> dict[str, Any]:
        try:
            results = memory.query_by_text(query) or []
        except Exception as e:  # pragma: no cover
            logger.warning(f"SceneGraphServer: query '{query}' failed: {e}")
            results = []
        items: list[dict[str, Any]] = []
        for result in results:
            metadata = result.get("metadata") or {}
            first = (
                metadata[0]
                if isinstance(metadata, list) and metadata
                else (metadata if isinstance(metadata, dict) else {})
            )
            items.append(
                {
                    "distance": result.get("distance"),
                    "position": (
                        [first.get("pos_x"), first.get("pos_y")]
                        if isinstance(first, dict)
                        else None
                    ),
                }
            )
        return {"query": query, "results": items}


def _make_handler(memory: SpatialMemorySpec) -> type[BaseHTTPRequestHandler]:
    """Build an HTTP handler that answers scene-graph queries from ``memory``."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlparse(self.path).path
            try:
                if path in ("/", "/index.html"):
                    self._html(_index_html())
                elif path == "/scene_graph":
                    self._json(SceneGraphServerModule._scene_graph_payload(memory))
                elif path == "/query":
                    query = _first(parse_qs(urlparse(self.path).query).get("q", []))
                    self._json(SceneGraphServerModule._query_payload(memory, query))
                elif path == "/health":
                    self._json({"ok": True})
                else:
                    self.send_response(404)
                    self.end_headers()
            except Exception as e:  # pragma: no cover - robust to any backend error
                logger.warning(f"SceneGraphServer: error handling {path}: {e}")
                self.send_response(500)
                self.end_headers()

        def _json(self, data: Any) -> None:
            body = json.dumps(data, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # Keep the logs quiet (the module logger is enough).
        def log_message(self, *args: Any) -> None:
            return

    return _Handler


def _first(values: list[str]) -> str:
    return values[0] if values else ""


def _index_html() -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Scene Graph</title>"
        "<style>body{font-family:system-ui;margin:2rem}pre{background:#f5f5f5;"
        "padding:1rem;overflow:auto}a{display:block;margin:.5rem 0}</style></head>"
        "<body><h1>Scene Graph</h1>"
        "<p>Live contents of the robot's scene graph.</p>"
        "<a href='/scene_graph'>/scene_graph (JSON)</a>"
        "<p>Ask the scene graph a question:</p>"
        "<form><input id='q' placeholder='e.g. the bed, a person, the kitchen'>"
        "<button type='submit' onclick="
        "fetch('/scene_graph').then(r=>r.json()).then(j=>document.getElementById('out').textContent=JSON.stringify(j,null,2))"
        "'>show</button></form>"
        "<pre id='out'></pre></body></html>"
    )
