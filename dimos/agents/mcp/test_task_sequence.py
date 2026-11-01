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

import asyncio
import json
from threading import Condition, Event

from jsonschema import ValidationError
import pytest

from dimos.agents.capabilities import CapabilityRegistry
from dimos.agents.mcp.mcp_server import _handle_tools_call, app, handle_request
from dimos.agents.mcp.task_sequence import TaskSequenceExecutor
from dimos.agents.skill_result import SkillResult
from dimos.core.module import SkillInfo


def outcome(success=True, message="finished"):
    return {"structuredContent": {"success": success, "message": message}}


def plan(tool="inspect", **kwargs):
    return {"tool": tool, "arguments": {"value": 1}, "completion": "result", **kwargs}


def registered(name="inspect", completion="result", lifecycle="instant", uses=()):
    return SkillInfo(
        class_name="Tools",
        func_name=name,
        args_schema=json.dumps(
            {
                "type": "object",
                "properties": {"value": {"type": "integer", "minimum": 1}},
                "required": ["value"],
            }
        ),
        completion=completion,
        lifecycle=lifecycle,
        uses=uses,
    )


@pytest.fixture
def executor_factory(tmp_path, mocker):
    resources = []
    releases = []

    def make(dispatch=None, tools=None, cancel=None, path=None):
        condition = Condition()
        snapshots = []

        def publish(status):
            with condition:
                snapshots.append(status)
                condition.notify_all()

        tools = tools or {"inspect": registered(), "record": registered("record")}
        executor = TaskSequenceExecutor(
            path or tmp_path / f"sequence-{len(resources)}.json",
            lambda: tools,
            dispatch or mocker.Mock(return_value=outcome()),
            cancel or mocker.Mock(return_value=False),
            publish,
        )
        resources.append(executor)

        def wait(predicate):
            with condition:
                assert condition.wait_for(
                    lambda: bool(snapshots) and predicate(snapshots[-1]), timeout=5
                ), snapshots
                return snapshots[-1]

        return executor, wait

    make.releases = releases
    yield make
    for release in releases:
        release.set()
    for executor in resources:
        executor.close()


def test_mixed_registered_tools_execute_in_order_once_and_persist(
    executor_factory, mocker, tmp_path
):
    calls = []
    path = tmp_path / "durable.json"

    def dispatch(name, arguments, operation_id):
        calls.append((name, arguments, operation_id))
        return outcome()

    executor, wait = executor_factory(dispatch, path=path)
    executor.start([plan(), plan("record"), plan()])
    status = wait(lambda state: state["status"] == "completed")
    assert [name for name, _, _ in calls] == ["inspect", "record", "inspect"]
    assert len({operation for _, _, operation in calls}) == 3
    assert [step["status"] for step in status["steps"]] == ["completed"] * 3
    assert json.loads(path.read_text()) == status
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "step",
    [
        plan(arguments={"value": "1"}),
        plan(arguments={"value": 0}),
        plan(arguments={"value": 1, "_mcp_context": {}}),
        plan(tool="missing"),
        plan(tool="start_task_sequence"),
        plan(completion="navigation"),
        plan(status="completed"),
    ],
)
def test_invalid_schema_or_permission_rejected_before_any_dispatch(executor_factory, mocker, step):
    dispatch = mocker.Mock()
    executor, _ = executor_factory(dispatch)
    with pytest.raises((ValueError, ValidationError)):
        executor.start([plan(), step])
    dispatch.assert_not_called()
    assert executor.status() == {"status": "idle"}


@pytest.mark.parametrize(
    "info",
    [registered(completion=None), registered(lifecycle="background")],
)
def test_unknown_or_open_ended_completion_contract_rejected(executor_factory, info):
    executor, _ = executor_factory(tools={"inspect": info})
    with pytest.raises(ValueError, match="completion contract"):
        executor.start([plan()])
    assert executor.status() == {"status": "idle"}


def test_started_text_is_not_success_and_blocks_uncertain_resume(executor_factory, mocker):
    dispatch = mocker.Mock(return_value={"content": [{"type": "text", "text": "Started"}]})
    executor, wait = executor_factory(dispatch)
    executor.start([plan(), plan("record")])
    status = wait(lambda state: state["status"] == "paused")
    assert status["active_index"] == 0
    assert "authoritative SkillResult" in status["reason"]
    assert status["quiescent"] is False
    with pytest.raises(ValueError, match="not confirmed stopped"):
        executor.resume(status["id"], retry_failed=True)
    assert dispatch.call_count == 1


def test_failure_pauses_and_resume_never_repeats_completed_steps(executor_factory, mocker):
    dispatch = mocker.Mock(side_effect=[outcome(), outcome(False, "blocked"), outcome(), outcome()])
    executor, wait = executor_factory(dispatch)
    executor.start([plan(), plan("record"), plan()])
    paused = wait(lambda state: state["status"] == "paused")
    assert paused["active_index"] == 1
    assert paused["steps"][0]["status"] == "completed"
    assert paused["steps"][1]["result"]["success"] is False
    # close joins the paused scheduler without changing completed progress.
    executor.close()
    with pytest.raises(ValueError, match="Explicit retry_failed"):
        executor.resume(paused["id"])
    executor.resume(paused["id"], retry_failed=True)
    completed = wait(lambda state: state["status"] == "completed")
    assert completed["active_index"] == 3
    assert [call.args[0] for call in dispatch.call_args_list] == [
        "inspect",
        "record",
        "record",
        "inspect",
    ]


def test_timeout_blocks_next_step_and_cannot_resume_while_call_is_alive(executor_factory, mocker):
    entered, release = Event(), Event()
    executor_factory.releases.append(release)

    def dispatch(*args):
        entered.set()
        assert release.wait(5)
        return outcome()

    executor, wait = executor_factory(dispatch)
    executor.start([plan(timeout=0.1), plan("record")])
    assert entered.wait(5)
    paused = wait(lambda state: state["status"] == "paused")
    assert paused["steps"][0]["status"] == "uncertain"
    assert "timed out" in paused["reason"]
    with pytest.raises(ValueError, match="not confirmed stopped"):
        executor.resume(paused["id"], retry_failed=True)
    release.set()
    final = wait(lambda state: state["status"] == "paused" and state["quiescent"])
    assert final["active_index"] == 1
    assert final["steps"][0]["status"] == "completed"
    assert final["steps"][1]["status"] == "pending"
    executor.close()
    executor.resume(final["id"])
    completed = wait(lambda state: state["status"] == "completed")
    assert completed["active_index"] == 2


def test_cancel_stops_active_operation_and_late_success_cannot_advance(executor_factory, mocker):
    entered, release = Event(), Event()
    executor_factory.releases.append(release)
    calls = []

    def dispatch(name, arguments, operation_id):
        calls.append(operation_id)
        entered.set()
        assert release.wait(5)
        return outcome()

    cancel = mocker.Mock(return_value=False)
    executor, wait = executor_factory(dispatch, cancel=cancel)
    executor.start([plan(), plan("record")])
    assert entered.wait(5)
    cancelled = executor.cancel()
    cancel.assert_called_once_with("inspect", calls[0])
    assert cancelled["status"] == "cancelled"
    release.set()
    final = wait(lambda state: state["status"] == "cancelled" and state["quiescent"])
    assert final["active_index"] == 1
    assert final["steps"][0]["result"]["success"] is True
    assert final["steps"][1]["status"] == "pending"
    assert len(calls) == 1


def test_restart_marks_inflight_uncertain_and_never_replays(executor_factory, mocker, tmp_path):
    path = tmp_path / "interrupted.json"
    entered, release = Event(), Event()
    executor_factory.releases.append(release)

    def dispatch(*args):
        entered.set()
        assert release.wait(5)
        return outcome()

    original, _ = executor_factory(dispatch, path=path)
    original.start([plan()])
    assert entered.wait(5)
    replacement_dispatch = mocker.Mock()
    recovered, _ = executor_factory(replacement_dispatch, path=path)
    status = recovered.status()
    assert status["status"] == "paused"
    assert status["steps"][0]["status"] == "uncertain"
    assert status["quiescent"] is False
    replacement_dispatch.assert_not_called()
    with pytest.raises(ValueError, match="not confirmed stopped"):
        recovered.resume(status["id"], retry_failed=True)


def test_mcp_dispatch_holds_movement_until_correlated_arrival_then_runs_next(
    executor_factory, mocker, monkeypatch
):
    entered, arrive = Event(), Event()
    navigation = registered("approach", completion="navigation", uses=("movement",))
    inventory = registered("inspect")
    tools = {"approach": navigation, "inspect": inventory}
    caps = CapabilityRegistry()
    rpc_calls = {
        "approach": mocker.Mock(return_value="Navigation started"),
        "inspect": mocker.Mock(return_value=SkillResult.ok("Counted tags")),
    }

    def navigation_status(operation_id):
        entered.set()
        return {
            "operation_id": operation_id,
            "status": "completed" if arrive.is_set() else "running",
        }

    monkeypatch.setattr(app.state, "skills_by_name", tools)
    monkeypatch.setattr(app.state, "cap_registry", caps)
    monkeypatch.setattr(app.state, "task_sequence", None)
    monkeypatch.setattr(app.state, "sequence_navigation_status", navigation_status)

    def dispatch(name, arguments, operation_id):
        response = asyncio.run(
            _handle_tools_call(
                1,
                {"name": name, "arguments": arguments},
                rpc_calls,
                sequence_operation_id=operation_id,
            )
        )
        return response["result"]

    executor, wait = executor_factory(dispatch, tools=tools)
    try:
        executor.start([plan("approach", completion="navigation"), plan()])
        assert entered.wait(5)
        assert caps.snapshot() == {"movement": "approach"}
        rpc_calls["inspect"].assert_not_called()
        assert executor.status()["active_index"] == 0
    finally:
        arrive.set()
    completed = wait(lambda state: state["status"] == "completed")
    assert completed["active_index"] == 2
    assert caps.snapshot() == {}
    assert rpc_calls["inspect"].call_count == 1


def test_mcp_stale_navigation_completion_id_cannot_advance(executor_factory, mocker, monkeypatch):
    navigation = registered("approach", completion="navigation")
    tools = {"approach": navigation}
    monkeypatch.setattr(app.state, "skills_by_name", tools)
    monkeypatch.setattr(app.state, "task_sequence", None)
    monkeypatch.setattr(
        app.state,
        "sequence_navigation_status",
        mocker.Mock(return_value={"operation_id": "old-invocation", "status": "completed"}),
    )

    def dispatch(name, arguments, operation_id):
        response = asyncio.run(
            _handle_tools_call(
                1,
                {"name": name, "arguments": arguments},
                {"approach": mocker.Mock(return_value="started")},
                sequence_operation_id=operation_id,
            )
        )
        return response["result"]

    executor, wait = executor_factory(dispatch, tools=tools)
    executor.start([plan("approach", completion="navigation")])
    status = wait(lambda state: state["status"] == "paused")
    assert status["active_index"] == 0
    assert status["steps"][0]["status"] == "failed"


def test_mcp_resume_is_not_an_agent_callable_tool(mocker):
    response = asyncio.run(
        handle_request(
            {"method": "tools/call", "id": 1, "params": {"name": "resume_task_sequence"}},
            [],
            {},
        )
    )
    assert response["result"]["isError"] is True
    assert "Tool not found" in response["result"]["content"][0]["text"]


def test_mcp_list_advertises_only_explicit_completion_contract():
    response = asyncio.run(
        handle_request(
            {"method": "tools/list", "id": 1},
            [registered(), registered("legacy", completion=None)],
            {},
        )
    )
    tools = response["result"]["tools"]
    assert tools[0]["_meta"]["dimos/completion"] == "result"
    assert "_meta" not in tools[1]


def test_mcp_manual_movement_preempts_before_dispatch(mocker, monkeypatch):
    sequence = mocker.Mock()
    calls = []
    sequence.pause.side_effect = lambda reason: calls.append("pause")
    move = mocker.Mock(side_effect=lambda **kwargs: calls.append("move") or SkillResult.ok())
    monkeypatch.setattr(app.state, "task_sequence", sequence)
    monkeypatch.setattr(
        app.state, "skills_by_name", {"manual_move": registered("manual_move", uses=("movement",))}
    )
    monkeypatch.setattr(app.state, "cap_registry", CapabilityRegistry())
    response = asyncio.run(
        _handle_tools_call(
            1, {"name": "manual_move", "arguments": {"value": 1}}, {"manual_move": move}
        )
    )
    assert calls == ["pause", "move"]
    assert response["result"]["structuredContent"]["success"] is True


def test_capability_refusal_is_terminal_without_dispatch_and_can_be_reviewed(
    executor_factory, mocker, monkeypatch
):
    caps = CapabilityRegistry()
    caps.acquire(["movement"], "following", "follow-token")
    tools = {"inspect": registered(uses=("movement",))}
    monkeypatch.setattr(app.state, "skills_by_name", tools)
    monkeypatch.setattr(app.state, "task_sequence", None)
    monkeypatch.setattr(app.state, "cap_registry", caps)
    monkeypatch.setattr(app.state, "cap_acquire_timeout", 0)
    call = mocker.Mock()

    def dispatch(name, arguments, operation_id):
        return asyncio.run(
            _handle_tools_call(
                1,
                {"name": name, "arguments": arguments},
                {"inspect": call},
                sequence_operation_id=operation_id,
            )
        )["result"]

    executor, wait = executor_factory(dispatch, tools=tools)
    executor.start([plan()])
    paused = wait(lambda state: state["status"] == "paused")
    assert paused["quiescent"] is True
    assert paused["steps"][0]["result"]["error_code"] == "INVALID_STATE"
    call.assert_not_called()


def test_cancellation_before_dispatch_commit_prevents_side_effect(executor_factory, mocker):
    entered, release = Event(), Event()
    executor_factory.releases.append(release)
    dispatch = mocker.Mock(return_value=outcome())
    executor, wait = executor_factory(dispatch)
    validate = executor._validate
    count = 0

    def delayed_validation(steps):
        nonlocal count
        count += 1
        if count == 2:
            entered.set()
            assert release.wait(5)
        return validate(steps)

    mocker.patch.object(executor, "_validate", side_effect=delayed_validation)
    executor.start([plan()])
    assert entered.wait(5)
    executor.cancel()
    release.set()
    cancelled = wait(lambda state: state["status"] == "cancelled" and state["quiescent"])
    assert cancelled["steps"][0]["result"]["message"] == "Cancelled before tool dispatch"
    dispatch.assert_not_called()


def test_large_results_preserve_terminal_outcome_without_unbounded_persistence(
    executor_factory, mocker
):
    dispatch = mocker.Mock(
        return_value={
            "structuredContent": {
                "success": True,
                "message": "Finished",
                "metadata": {"image": "x" * 100_000},
            }
        }
    )
    executor, wait = executor_factory(dispatch)
    executor.start([plan()])
    completed = wait(lambda state: state["status"] == "completed")
    assert completed["steps"][0]["result"] == {
        "success": True,
        "message": "Finished",
        "error_code": "",
        "metadata": {"result_truncated": True},
    }


def test_plan_step_count_and_duplicate_ids_are_rejected_before_dispatch(executor_factory, mocker):
    dispatch = mocker.Mock()
    executor, _ = executor_factory(dispatch)
    with pytest.raises(ValueError):
        executor.start([plan()] * 33)
    with pytest.raises(ValueError, match="unique"):
        executor.start([plan(id="duplicate"), plan(id="duplicate")])
    dispatch.assert_not_called()
