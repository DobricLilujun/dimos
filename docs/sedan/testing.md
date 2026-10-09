# Testing & verification

This section explains **how to verify** the DimOS Agents additions, what is
verifiable **automatically** versus what needs a **real Go2**, and how to build
this documentation site.

> **Environment note.** The verification available in a *docs-only* checkout is
> static + build based: **ruff** (lint/format) and **mkdocs build**. The full
> **pytest** / **mypy** / **on-hardware** suites need the project's dev
> environment (and, for hardware flows, a Go2). This page separates the two
> clearly so a result is never over-claimed.

## 1. Static checks (run in the docs environment)

```bash
# Lint / format the new modules and the docs
ruff check dimos/web/console/ dimos/perception/experimental/ \
  dimos/agents/skills/navigation.py dimos/agents/skills/person_recognition.py \
  dimos/agents/skills/scene_graph_server.py

ruff format --check dimos/

# Build the GitHub Pages site (proves the docs render + links resolve)
uv run --no-sync mkdocs build --config-file mkdocs.github-pages.yml
# or, with the docs group installed:
source .venv/bin/activate
mkdocs build --config-file mkdocs.github-pages.yml
```

- `ruff` passing = the new Python is clean (no unused imports/vars, formatting
  matches).
- `mkdocs build` succeeds with relative links (compatible with the `/dimos/`
  project-pages path) — no broken internal links.

## 2. The console's offline tests

The web console ships **offline** tests that isolate the robot, LLM, and
lifecycle boundaries. They do **not** start a real robot or call the cloud
API. They verify:

- UI / HTTP / SSE behaviour
- parameter handling and settings save
- key handling
- the lifecycle (start / stop / SIGTERM escalation)

These are run as part of the pytest suite. **They cannot** prove
real-hardware navigation or vision — that requires a hardware acceptance run
(§3).

### Person following and the console stacks

Also offline (no robot, no simulator, no cloud calls): the stack profiles, every launch
command checked against its real blueprint, readiness, the simulated scene folders,
which operations each stack shows and the server refuses, Stop and keyboard takeover
ending a follow, and the live follow-distance route and its validation. Run with
`pytest dimos/web/console dimos/agents/skills/test_person_navigation.py
dimos/simulation/mujoco dimos/navigation/go2/replanning_a_star`.

The browser tests (`test_console_browser.py`, the `web_browser` marker) need Playwright
and are not part of the default run. The page's JavaScript can only be syntax-checked
without a browser, so check the selectors, the People buttons and the sliders by hand
(see [the web console](web-console.md#choosing-a-stack-and-a-connection)). Starting
MuJoCo needs a display; the person skills need a vision-model key and cost API calls.

## 3. Real-hardware acceptance (needs a Go2)

Because the offline tests deliberately avoid the robot and the cloud, these
must be checked on real hardware:

| Flow | Check |
|---|---|
| **Alignment** | In a clear, safe, supervised area, capture a restored scan and confirm the **actual** match by eye (walls, corners, position, heading). Do **not** trust a high fitness alone. |
| **Navigation** | `navigate_to_memory_tag` / `navigate_near_memory_tag` actually move the robot to the tag; "started navigating" is not arrival; `stop_navigation` / `Esc` stop it. |
| **Person recognition** | A gallery person is detected and spoken; the cooldown works. |
| **Go2 speaker / Puppy** | The Go2 **accepts** the speaker and mic; TTS plays; the mic conversation and ambient commentary behave as documented. Firmware support is not covered by offline tests. |
| **Fusion gate** | At rest the accepted-frame count stops growing; save does not keep growing the map; normal motion resumes. |
| **PGO** | In a supervised loop, the current-run map/tags/pose are corrected and navigation resumes after sync; a wrong ICP loop is caught (not auto-trusted). |

## 4. Manual verification cheat-sheet

| Goal | Command / action |
|---|---|
| Stack up? | `dimos status` |
| Agent live messages | `dimos agentspy` |
| MCP tools present | `dimos mcp list-tools` (restart the stack after code changes) |
| All tags | `dimos mcp call query_memory_tags` |
| Navigate | `dimos agent-send "Navigate to the fire extinguisher."` |
| Cancel navigation | `dimos mcp call stop_navigation` |
| Save map | `dimos shell` → `app.PersistentGo2Map.save_map()` |
| Alignment status | `dimos shell` → `app.PersistentGo2Map.alignment_status()` |
| Follow log | `dimos log -f` |

## 5. Limits (do not over-claim)

- Offline tests **do not** start a real robot or call the cloud API, so they
  cannot prove real-hardware navigation, vision, speaker, or mic acceptance.
- Alignment/fitness is a **match metric, not correctness** — symmetric
  corridors can score high with the wrong heading; always confirm by eye.
- PGO and ICP are **not** a guarantee of a correct global pose; a wrong startup
  alignment is not auto-fixed.
- **Never treat a chat, button, or "stop" action as a hardware emergency
  stop.** Use the physical remote / hardware stop.

## 6. Building the documentation site

```bash
# Local build (project-relative links; good for previewing)
mkdocs build --config-file mkdocs.yml

# GitHub Pages build (absolute /dimos/ links; used by the deploy workflow)
mkdocs build --config-file mkdocs.github-pages.yml
```

The GitHub Pages site is deployed by
[`.github/workflows/pages.yml`](https://github.com/DobricLilujun/dimos)
to `https://dobriclilujun.github.io/dimos`. It runs `uv sync --only-group docs`
(installs the docs toolchain only — no heavy robot deps), builds with
`mkdocs.github-pages.yml`, pulls the LFS media, uploads the artifact, and
deploys to the `gh-pages` branch.

### What the deploy workflow cannot do here

The full `pytest` / `mypy` suites need the project's dev environment
(`uv sync --all-groups`) and, for `self_hosted` / `mujoco`-marked tests, the
self-hosted runner. In a docs-only checkout they are **not** run — this is why
verification here is ruff + mkdocs build + the documented offline console tests,
not the full suite.

## Related

- [The web console](web-console.md)
- [Quick start: the persistent workflow](quickstart.md)
- [CLI reference](cli.md)
