# Scene graph server

`SceneGraphServerModule` exposes the robot's **scene graph** over a minimal HTTP
"dialog" interface, so an operator can query it from any browser or `curl`. It
reuses the native scene map / scene graph built by `SpatialMemory` (queried
through `SpatialMemorySpec`) and the robot's own web-server pattern
(`WebInput` / `FastAPIServer`).

## Opt-in

The server is a **no-op** until a port is set, so the `unitree-go2-agentic`
blueprint is unchanged until:

```bash
--scene-graph-server.port <n>
```

## HTTP surface

| Endpoint | Returns |
|---|---|
| `GET /` | A minimal HTML page that lists the scene graph |
| `GET /scene_graph` | JSON of named locations + map stats |
| `GET /query?q=...` | Interactive: query the scene graph with a natural-language string and return the best matching items (positions included) |

Example:

```bash
curl http://127.0.0.1:<port>/query?q=fire\ extinguisher
curl http://127.0.0.1:<port>/scene_graph
```

## Configuration

| Flag | Default | Meaning |
|---|---|---|
| `port` | `None` (disabled) | HTTP port to serve on |
| `host` | `127.0.0.1` | Bind host (localhost only — do not expose to the public internet) |

The server runs on a background `ThreadingHTTPServer`; `stop()` joins the thread
(bounded by `DEFAULT_THREAD_JOIN_TIMEOUT`) and closes the server.

## How it relates to the other features

- It queries the **same** `SpatialMemory` that the agent and
  `query_memory_tags` read, so its answers stay consistent with what the agent
  sees.
- The [web console](web-console.md) **Query memory** button and the agent's
  `query_memory_tags` skill are the *control-deck / chat* equivalents; this
  server is a *programmatic / curl* equivalent.
- It does not itself move the robot — it only reports the scene graph.
  Navigation is done through the agent or `navigate_to_memory_tag`.

## Related

- [The web console](web-console.md)
- [Quick start: the persistent workflow](quickstart.md)
