# Merge summary: `learning-branch` onto `upstream/main`

Branch `learning-branch-sync` is rebased onto the latest
`upstream/main` and re-applies the branch's work as a minimal, reviewable
diff. 86 `dimos/` code files changed (≈15.5k insertions, 93 deletions).

## Guiding principles
- Do **not** rewrite upstream code. Only touch dead code, simplifiable code,
  or code this branch must preserve.
- Keep behaviour unless an upstream conflict forces it or code is confirmed dead.
- No unrelated formatting / renaming / refactoring.
- Branch-only docs and GitHub Pages are **deferred to a separate PR**.

## Kept (branch features that are compatible with upstream)
- **Person following / recognition**: `person_navigation.py`, `person.py`,
  `reply_speaker.py`, `cute_phrases.py`, `sit_observer.py`, `mode_announcer.py`.
- **Scene graph / spatial perception**: `scene_graph_server.py`,
  `spatial_perception.py`, `object_segmentation.py`, `visual_memory.py`,
  `vlm_caption_provider.py`, `tag_view.py`.
- **Re-localization (Go2 persistent map)**: `relocalization/go2/persistent.py`
  and the `unitree-go2-agentic-persistent(-demo)` blueprints.
- **Navigation goal tracking / voice**: `goal_tracker.py`, `node_vad.py`
  (added `calculate_rms_volume` back to `volume.py`), `local_planner.py`
  branch bits merged with upstream.
- **Specs**: added `PersonMemorySpec` next to upstream's `SpatialMemorySpec`
  so person skills resolve without breaking upstream's `test_navigation.py`.
- **Config**: `openai_api_key` added back to `GlobalConfig` (used by person
  navigation + spatial perception).

## Taken from upstream (rewritten core — branch version dropped)
`mcp_server.py`, `skills/navigation.py`, `loop_closure/pgo_map.py`,
`go2/connection.py`, `skill_result.py`, `voxel_grid.py`,
`replanning_a_star/local_planner.py`, `spatial_memory_spec.py`
(upstream's smaller `SpatialMemorySpec`; branch's extra methods removed),
`test_navigation.py`, `test_connection.py`, `test_depth_camera.py`,
`test_pgo_map.py`, `test_mcp_server.py`, `module.py` (upstream's safer
names loop + branch's `completion` field).

## Deleted (dead / incompatible)
- **`dimos/web/console/`** (the SEDAN web console, 14 files) — needs ~15
  branch-only config fields across many modules (VLM, person-following,
  spatial memory, goal tracker, `puppy_*`); incompatible with upstream.
  The `persistent-console` blueprint variant was reduced to just the demo
  stack (which the person-following blueprint reuses).
- **Branch tests for dropped/removed contracts**: `test_puppy_connection.py`
  (puppy connection logic was in the branch's `connection.py`, replaced by
  upstream's), the removed-`SkillResult.ok` cases in `test_task_sequence.py`,
  and `test_annotation.py` (replaced with upstream's, which matches upstream's
  `SkillResult`).
- **Branch-only docs** (`docs/` → `docs_base/` rebrand, top-level `.md`,
  `mkdocs.github-pages.yml`, `pages.yml`) — deferred to a separate PR.
- Dead data: `assets/scene_maps/` journal+test data, `plan/`, `notebook/`,
  `misc/mkdocs/changelog.py`.

## Test results (`uv run pytest`, fast suite)
- **7711 passed, 29 failed, 35 errors, 31 skipped**.

### Pre-existing / environmental (not introduced by this merge)
- LFS pull failures (`test_data`, `test_odometry`, `test_OccupancyGrid`,
  `test_goal_validator`, `test_min_cost_astar`, …) — fail identically on a
  clean `upstream/main` checkout.
- `test_mcp_client::test_image` — fails on clean `upstream/main` too (flaky
  vision answer); not introduced here.
- `test_all_blueprints_generation.py` — only complains about uncommitted
  changes; passes once the generated file is committed.

### Introduced by this merge (decision points)
- `test_unitree_skill_container::test_pounce` — passes on upstream; the branch
  `UnitreeSkillContainer` adds a `_spatial_memory: SpatialMemorySpec` ref that
  upstream's test mock does not provide. **Decision:** keep the branch skills
  (`go_to_place`/`go_to_object`) and accept the 1 failure, or revert
  `unitree_skill_container.py` to upstream.
- Branch-added tests for features whose module was replaced by upstream's
  version: `test_persistent.py` (1), `navigation/.../test_module.py` (6,
  expects `_global_config` on the branch `GlobalPlanner`),
  `perception/.../test_scene_map_config.py` (2, expects branch `find_robot_location`),
  `replanning_a_star/test_local_planner.py` (2 errors, calls the branch
  `LocalPlanner.__init__` signature), 5 removed-`SkillResult` cases in
  `test_task_sequence.py`.
- Code-style checks (`test_no_sections`, `test_inline_heavy_imports`,
  `test_get_logger`) — branch code / vendored files flagged; pass on clean
  upstream.

## Remaining risks
- The dropped **web console** and **puppy connection** features are not
  ported; re-enabling them requires re-adding branch config to upstream
  modules (a larger, non-minimal change).
- The `test_task_sequence.py` feature (`mcp_client`) works, but 5 of its
  tests assert the removed `SkillResult.ok` contract.
