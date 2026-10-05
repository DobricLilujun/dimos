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

"""Browser contracts over real HTTP/SSE, with no robot or model calls."""

import asyncio
from contextlib import asynccontextmanager
import threading
import time

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
import numpy as np
import pytest
import uvicorn

from dimos.msgs.sensor_msgs.Image import Image
from dimos.web.console.module import RobotConsoleModule
from dimos.web.console.settings import ConsoleRuntime

pytestmark = pytest.mark.web_browser


@pytest.fixture
def console_server(tmp_path, mocker):
    module = RobotConsoleModule()
    runtime = ConsoleRuntime(tmp_path, tmp_path / "console.json")
    module.runtime = runtime

    def apply_settings(settings):
        module.config.rerun_web_port = settings.rerun_web_port
        module.config.rerun_grpc_port = settings.rerun_grpc_port

    runtime.on_settings = apply_settings
    runtime.on_event = module._emit
    calls = []
    child = mocker.Mock(pid=123456)
    child.poll.return_value = None

    async def rpc(op, args):
        if op.key == "nearby_navigation_status":
            return {"ok": True, "result": {"distance_m": 1.0}}
        if op.key == "visual_arrival_status":
            return {"ok": True, "result": {"enabled": False, "searching": False}}
        if op.key == "reply_speaker_status":
            return {
                "ok": True,
                "result": {
                    "enabled": False,
                    "puppy_configured": True,
                    "puppy": None,
                    "microphone": False,
                },
            }
        calls.append((op.key, args))
        if op.key == "set_nearby_arrival_distance":
            return {"ok": True, "result": args}
        if op.key == "configure_visual_arrival":
            return {"ok": True, "result": {**args, "searching": False}}
        return {"ok": True, "result": "Ready:" if op.key == "alignment_status" else True}

    async def mcp(op, args):
        calls.append((op.key, args))
        result = {
            "matching_tag_count": 1,
            "total_stored_tags": 1,
            "tags": [{"id": "loc_office", "name": "office", "position": [1, 2, 0]}],
        }
        return {"ok": True, "result": result if op.key == "query_memory_tags" else "done"}

    def start(overwrite_token=None):
        child.poll.return_value = None
        runtime._process = child
        runtime._state = "running"
        return runtime.status()

    def stop():
        child.poll.return_value = 0
        runtime._state = "stopped"
        return runtime.status()

    async def status():
        return {**module._status, "stack": runtime.status()}

    def publish(text):
        module._on_agent(HumanMessage(content=text))
        module._on_agent(
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "call-1", "name": "query_memory_tags", "args": {"query": "office"}}
                ],
            )
        )
        module._on_agent(
            ToolMessage(content="found office", tool_call_id="call-1", name="query_memory_tags")
        )
        module._on_agent(AIMessage(content="Office is saved. I did not move."))
        module._on_idle(True)

    mocker.patch.object(module, "_call_rpc", side_effect=rpc)
    mocker.patch.object(module, "_call_mcp", side_effect=mcp)
    mocker.patch.object(module, "_refresh_status", side_effect=status)
    mocker.patch.object(module, "_publish_human_input", side_effect=publish)
    mocker.patch.object(runtime, "start", side_effect=start)
    mocker.patch.object(runtime, "stop", side_effect=stop)
    module._status.update(
        agent_idle=True, navigation_ready=True, alignment_status="Ready: sensors use world frame."
    )

    @asynccontextmanager
    async def lifespan(app):
        module._console_loop = asyncio.get_running_loop()
        yield
        module._console_loop = None

    app = module._build_app()
    app.router.lifespan_context = lifespan
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=server.run, name="console-browser-server", daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if time.monotonic() >= deadline:
                raise AssertionError("Browser console did not start")
            thread.join(timeout=0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        module.config.port = port
        yield f"http://127.0.0.1:{port}", module, runtime, calls
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        module._close_module()
        assert not thread.is_alive()


@pytest.fixture
def page(console_server):
    # Optional browser-tests dependency; default suite must collect without it.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(console_server[0])
            page.locator("#b-conn").get_by_text("connected", exact=True).wait_for()
            yield page
        finally:
            browser.close()


def start_stack(page):
    page.locator("#stack-start").click()
    page.locator("#operation-form button[type=submit]").click()
    page.locator("#b-stack").get_by_text("running", exact=True).wait_for()


def test_settings_save_all_workflow_parameters_without_exposing_keys(page, console_server):
    runtime = console_server[2]
    (runtime.project_dir / ".env").write_text(
        f"OPENAI_API_KEY=test-only-key\nUNITREE_AES_128_KEY={'0' * 32}\n"
    )
    page.locator("#settings-open").click()
    page.locator("#setting-robot_ip").fill("192.168.63.218")
    for key in ("openai_api_key", "unitree_aes_128_key"):
        assert page.locator(f"#setting-{key}").get_attribute("readonly") is not None
        assert page.locator(f"#setting-{key}").input_value() == "********"
    page.locator("#setting-agent_url").fill("http://127.0.0.1:8000/v1")
    page.locator("#setting-agent_model").fill("openai:local-model")
    page.locator("#setting-vlm_url").fill("http://127.0.0.1:8000")
    page.locator("#setting-vlm_model").fill("local-vision")
    page.locator("#setting-map_mode").select_option("new")
    page.locator("#setting-scene_map_dir").fill("assets/scene_maps/browser_test")
    page.locator("#setting-replay").check()
    page.locator("#setting-place_tagging").uncheck()
    page.locator("#setting-pgo_enabled").check()
    page.locator("#setting-vlm_distance_m").fill("2.5")
    page.locator("#setting-rerun_grpc_port").fill("9887")
    page.locator("#settings-save").click()
    page.locator("#settings-dialog").wait_for(state="hidden")
    runtime = console_server[2]
    assert runtime.settings.robot_ip == "192.168.63.218"
    assert runtime.settings.agent_model == "openai:local-model"
    assert runtime.settings.vlm_distance_m == 2.5
    assert runtime.settings.place_tagging is False
    assert runtime.settings.pgo_enabled is True
    assert runtime.settings.replay is True
    assert runtime.settings.rerun_grpc_port == 9887
    assert page.locator("#rr").get_attribute("src") == (
        "http://127.0.0.1:9878/?url=rerun%2Bhttp%3A%2F%2F127.0.0.1%3A9887%2Fproxy"
        f"&url=http%3A%2F%2F127.0.0.1%3A{console_server[1].config.port}%2Fapi%2Fview%2F3d.rbl"
    )
    assert "test-only-key" not in runtime.settings_path.read_text()
    page.locator("#settings-open").click()
    page.locator("#settings-dialog").wait_for(state="visible")
    assert page.locator("#setting-pgo_enabled").is_checked()
    assert page.locator("#setting-openai_api_key").input_value() == "********"
    assert (
        page.locator("#setting-openai_api_key").get_attribute("placeholder") == "Configured in .env"
    )


def test_start_and_alignment_require_explicit_confirmation(page, console_server):
    runtime = console_server[2]
    page.locator("#stack-start").click()
    page.locator("#operation-cancel").click()
    assert runtime.start.call_count == 0
    start_stack(page)
    button = page.locator('[data-operation="confirm_alignment"]')
    button.click()
    page.locator("#operation-cancel").click()
    assert console_server[3] == []
    button.click()
    with page.expect_response("**/api/action") as response:
        page.locator("#operation-form button[type=submit]").click()
    assert response.value.json()["ok"] is True
    assert console_server[3] == [("confirm_alignment", {})]


@pytest.mark.parametrize(
    "key,parameter,value",
    [
        ("tag_object", "object_name", "chair"),
        ("tag_location", "location_name", "office"),
        ("query_memory_tags", "query", "office"),
        ("navigate_near_memory_tag", "location_id", "loc_office"),
        ("navigate_to_memory_tag", "location_id", "loc_office"),
    ],
)
def test_parameter_forms_submit_actual_skill_arguments(page, console_server, key, parameter, value):
    start_stack(page)
    page.locator(f'[data-operation="{key}"]').click()
    page.locator(f'#operation-fields input[name="{parameter}"]').fill(value)
    with page.expect_response("**/api/action"):
        page.locator("#operation-form button[type=submit]").click()
    assert console_server[3] == [(key, {parameter: value})]
    if key == "query_memory_tags":
        assert page.locator("#inventory").inner_text().find("loc_office") >= 0
        page.locator("#inventory button").click()
        assert (
            page.locator('#operation-fields input[name="location_id"]').input_value()
            == "loc_office"
        )


def test_chat_pairs_tool_io_without_duplicate_user_and_idle_is_not_thinking(page):
    start_stack(page)
    page.locator("#input").fill("Query office; do not move.")
    page.locator("#input").press("Enter")
    page.get_by_text("Office is saved. I did not move.", exact=True).wait_for()
    assert page.locator(".msg.user").count() == 1
    assert page.locator(".tool").count() == 1
    assert page.locator(".tool .in pre").inner_text() == '{\n  "query": "office"\n}'
    assert page.locator(".tool .out pre").inner_text() == "found office"
    assert page.locator("#b-agent").inner_text() == "agent idle"
    assert not page.locator("#b-thinking").is_visible()


def test_mobile_layout_keeps_settings_and_chat_accessible(page):
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("#settings-open").click()
    page.locator("#settings-dialog").wait_for(state="visible")
    assert page.locator("#settings-dialog").is_visible()
    assert page.locator("#setting-robot_ip").is_visible()


def test_long_chat_stays_within_desktop_viewport(page, console_server):
    start_stack(page)
    before = page.locator(".chat").bounding_box()
    console_server[1]._on_agent(AIMessage(content=("A long response.\n" * 1000)))
    page.locator(".msg.agent").wait_for()
    after = page.locator(".chat").bounding_box()
    assert before == after
    assert page.evaluate("document.documentElement.scrollHeight <= innerHeight")
    assert page.locator("#msgs").evaluate("(node)=>node.scrollHeight>node.clientHeight")
    assert page.locator("#input").is_visible()


def test_camera_and_3d_swap_without_reloading_viewer(page, console_server):
    start_stack(page)
    console_server[1]._on_camera(Image.from_numpy(np.zeros((12, 16, 3), dtype=np.uint8)))
    page.locator("#camera-image").wait_for(state="visible")
    assert page.locator("#world-view").get_attribute("class") == "stream-view"
    source = page.locator("#rr").get_attribute("src")
    page.locator("#camera-switch").click()
    assert page.locator("#camera-view").get_attribute("class") == "stream-view"
    assert page.locator("#world-view").get_attribute("class") == "stream-view pip"
    page.locator("#world-switch").click()
    assert page.locator("#world-view").get_attribute("class") == "stream-view"
    assert page.locator("#rr").get_attribute("src") == source


def test_fullscreen_enters_and_exits_without_hiding_controls(page):
    page.locator("#fullscreen-toggle").click()
    page.wait_for_function("document.fullscreenElement !== null")
    page.get_by_role("button", name="Exit full screen", exact=True).wait_for()
    assert page.locator("#settings-open").is_visible()
    page.locator("#fullscreen-toggle").click()
    page.wait_for_function("document.fullscreenElement === null")
    page.get_by_role("button", name="Full screen", exact=True).wait_for()


def test_nearby_distance_slider_updates_next_trip_threshold(page, console_server):
    start_stack(page)
    slider = page.get_by_role("slider", name="Nearby stop distance", exact=True)
    slider.fill("1.7")
    with page.expect_response("**/api/navigation-distance") as response:
        slider.dispatch_event("change")
    assert response.value.json() == {"ok": True, "result": {"distance_m": 1.7}}
    assert page.locator("#navigation-distance-value").inner_text() == "1.7 m"
    assert ("set_nearby_arrival_distance", {"distance_m": 1.7}) in console_server[3]


def test_visual_arrival_toggle_requires_rotation_consent(page, console_server):
    start_stack(page)
    page.locator("#visual-arrival-toggle").click()
    page.get_by_role("heading", name="Enable visual arrival search", exact=True).wait_for()
    assert not any(key == "configure_visual_arrival" for key, _ in console_server[3])
    with page.expect_response("**/api/visual-arrival") as response:
        page.locator("#operation-form button[type=submit]").click()
    assert response.value.json()["result"]["enabled"] is True
    page.get_by_role("button", name="Visual arrival: On", exact=True).wait_for()
    assert ("configure_visual_arrival", {"enabled": True}) in console_server[3]


def test_murmur_has_separate_toggle_and_shows_camera_error(page, console_server, mocker):
    enabled = [True]

    async def rpc(op, args):
        if op.key == "nearby_navigation_status":
            return {"ok": True, "result": {"distance_m": 1.0}}
        if op.key == "visual_arrival_status":
            return {"ok": True, "result": {"enabled": False, "searching": False}}
        if op.key == "configure_puppy_murmur":
            enabled[0] = args["enabled"]
        return {
            "ok": True,
            "result": {
                "enabled": True,
                "generation": 1,
                "puppy_configured": True,
                "microphone": True,
                "camera_age_s": 0.1,
                "puppy": {
                    "running": True,
                    "murmur": enabled[0],
                    "model": "gpt-4o-mini",
                    "last_error": "Vision API unavailable",
                    "busy": False,
                },
            },
        }

    mocker.patch.object(console_server[1], "_call_rpc", side_effect=rpc)
    start_stack(page)
    page.get_by_role("button", name="Murmur: On", exact=True).wait_for()
    assert "Vision API unavailable" in page.locator("#puppy-status").inner_text()
    with page.expect_response(
        lambda response: response.url.endswith("/api/murmur") and response.request.method == "POST"
    ) as response:
        page.locator("#murmur-toggle").click()
    assert response.value.json()["result"]["puppy"]["murmur"] is False
    page.get_by_role("button", name="Murmur: Off", exact=True).wait_for()
    assert "Puppy mic ON" in page.locator("#speaker-toggle").inner_text()


def test_old_stack_cannot_claim_murmur_is_running(page, console_server, mocker):
    async def rpc(op, args):
        if op.key == "nearby_navigation_status":
            return {"ok": True, "result": {"distance_m": 1.0}}
        if op.key == "visual_arrival_status":
            return {"ok": True, "result": {"enabled": False, "searching": False}}
        return {"ok": True, "result": {"enabled": True, "generation": 3}}

    mocker.patch.object(console_server[1], "_call_rpc", side_effect=rpc)
    start_stack(page)
    page.wait_for_function(
        "document.querySelector('#puppy-status').textContent.includes('Old/non-Puppy stack')"
    )
    assert page.locator("#murmur-toggle").is_disabled()


def test_keyboard_ui_requires_space_and_stops_on_release_and_blur(page, console_server, mocker):
    start_stack(page)
    transport = mocker.patch("dimos.web.console.module.make_transport").return_value
    moving = threading.Event()
    stopped = threading.Event()
    commands = []

    def publish(command):
        commands.append(command)
        if command.linear.x == 0.3:
            moving.set()
        elif moving.is_set():
            stopped.set()

    transport.publish.side_effect = publish
    page.locator("#keyboard-enable").click()
    page.locator("#operation-form button[type=submit]").click()
    page.get_by_role("button", name="Disable keyboard", exact=True).wait_for()
    page.keyboard.down("Space")
    page.keyboard.down("w")
    assert moving.wait(3)
    page.keyboard.up("Space")
    assert stopped.wait(3)
    assert commands[-1].linear.x == 0
    moving.clear()
    stopped.clear()
    page.keyboard.up("w")
    button = page.locator('[data-drive="w"]')
    button.hover()
    page.mouse.down()
    assert moving.wait(3)
    page.mouse.up()
    assert stopped.wait(3)
    assert commands[-1].linear.x == 0
    page.evaluate("window.dispatchEvent(new Event('blur'))")
    page.get_by_role("button", name="Enable keyboard", exact=True).wait_for()


def test_runtime_tagging_toggle_and_starting_location_button(page, console_server, mocker):
    start_stack(page)
    enabled = [True]

    async def rpc(op, args):
        if op.key == "set_automatic_tagging":
            enabled[0] = args["enabled"]
        return {"ok": True, "result": {"enabled": enabled[0], "configured": True}}

    mocker.patch.object(console_server[1], "_call_rpc", side_effect=rpc)
    page.locator("#tagging-toggle").click()
    page.get_by_role(
        "button", name="Automatic tagging: On (click to toggle)", exact=True
    ).wait_for()
    page.locator("#tagging-toggle").click()
    page.get_by_role(
        "button", name="Automatic tagging: Off (click to toggle)", exact=True
    ).wait_for()
    assert enabled[0] is False
    page.locator('[data-operation="query_starting_location"]').click()
    page.locator("#log").get_by_text("query_starting_location", exact=True).first.wait_for()
    assert ("query_starting_location", {}) in console_server[3]


def test_go2_speaker_requires_max_volume_confirmation_and_can_be_disabled(
    page, console_server, mocker
):
    start_stack(page)
    enabled = [False]

    async def rpc(op, args):
        if op.key == "nearby_navigation_status":
            return {"ok": True, "result": {"distance_m": 1.0}}
        if op.key == "visual_arrival_status":
            return {"ok": True, "result": {"enabled": False, "searching": False}}
        if op.key == "configure_reply_speaker":
            enabled[0] = args["enabled"]
        return {
            "ok": True,
            "result": {
                "enabled": enabled[0],
                "generation": 1,
                "puppy_configured": True,
                "microphone": enabled[0],
                "camera_age_s": 0.2,
                "puppy": {
                    "running": True,
                    "murmur": True,
                    "model": "gpt-4o-mini",
                    "last_error": None,
                    "busy": False,
                }
                if enabled[0]
                else None,
            },
        }

    mocker.patch.object(console_server[1], "_call_rpc", side_effect=rpc)
    page.locator("#speaker-toggle").click()
    page.get_by_role("heading", name="Enable Go2 speaker + Puppy murmur", exact=True).wait_for()
    assert enabled[0] is False
    page.locator("#operation-form button[type=submit]").click()
    page.get_by_role("button", name="Go2 speaker: On (max) · Puppy mic ON", exact=True).wait_for()
    page.locator("#speaker-toggle").click()
    page.get_by_role("button", name="Go2 speaker: Off", exact=True).wait_for()
    assert enabled[0] is False


def test_existing_scene_requires_overwrite_confirmation_before_start(page, console_server):
    runtime = console_server[2]
    runtime.settings = runtime.settings.model_copy(
        update={"map_mode": "new", "scene_map_dir": "old_scene"}
    )
    scene = runtime.project_dir / "old_scene"
    scene.mkdir()
    (scene / "tags.json").write_text("old tags")
    page.locator("#stack-start").click()
    page.get_by_role("heading", name="Overwrite existing scene?").wait_for()
    page.locator("#operation-cancel").click()
    assert runtime.start.call_count == 0
    assert (scene / "tags.json").read_text() == "old tags"

    page.locator("#stack-start").click()
    page.get_by_role("heading", name="Overwrite existing scene?").wait_for()
    page.locator("#operation-form button[type=submit]").click()
    page.get_by_role("heading", name="Start robot stack", exact=True).wait_for()
    page.locator("#operation-form button[type=submit]").click()
    page.locator("#b-stack").get_by_text("running", exact=True).wait_for()
    runtime.start.assert_called_once_with(runtime._overwrite_token)


def test_stop_keeps_console_open_and_disables_robot_controls(page, console_server):
    start_stack(page)
    page.locator("#stack-stop").click()
    page.locator("#operation-form button[type=submit]").click()
    page.locator("#b-stack").get_by_text("stopped", exact=True).wait_for()

    assert console_server[2].stop.call_count == 1
    assert page.locator("#settings-open").is_enabled()
    assert page.locator("#stack-start").is_enabled()
    assert page.locator("#input").is_disabled()
    assert page.locator('[data-operation="tag_object"]').is_disabled()


def test_failed_operation_and_live_stack_logs_are_visible(page, console_server, mocker):
    start_stack(page)
    mocker.patch.object(
        console_server[1],
        "_call_mcp",
        return_value={"ok": False, "error": "Vision service unavailable"},
    )
    page.locator('[data-operation="tag_object"]').click()
    page.locator('#operation-fields input[name="object_name"]').fill("chair")
    page.locator("#operation-form button[type=submit]").click()
    page.locator("#log").get_by_text("Vision service unavailable", exact=False).wait_for()

    console_server[1]._emit({"type": "stack_log", "text": "test stack log line"})
    page.locator("#stack-log").get_by_text("test stack log line", exact=False).wait_for()
    assert "Vision service unavailable" in page.locator("#log .err").first.inner_text()
    assert "test stack log line" in page.locator("#stack-log").inner_text()
    assert page.locator("#stack-logs").is_visible()
    viewer = page.locator(".viewer").bounding_box()
    console = page.locator("#stack-logs").bounding_box()
    assert viewer is not None and console is not None
    assert console["y"] >= viewer["y"] + viewer["height"]
    page.locator("#console-clear").click()
    assert page.locator("#stack-log").inner_text() == ""
