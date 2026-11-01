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

"""One durable, fail-closed workflow over registered MCP tools.

The dispatcher remains the authority for capabilities and permissions. A result
contract is an explicit promise by the skill author, not inferred from prose.
Uninterruptible calls retain ownership until they return; a timeout is never
evidence that their side effects stopped.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
import copy
import json
import os
from pathlib import Path
from threading import Event, RLock, Thread
import time
from typing import Any, Literal
import uuid

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from dimos.core.module import SkillInfo
from dimos.utils.logging_config import setup_logger

logger = setup_logger()

MAX_STATE_BYTES = 256_000
SEQUENCE_TOOL_NAMES = {
    "start_task_sequence",
    "task_sequence_status",
    "pause_task_sequence",
    "cancel_task_sequence",
    "resume_task_sequence",
}


class SequenceStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1, max_length=100)
    tool: str = Field(min_length=1, max_length=200)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    completion: Literal["result", "navigation"]
    timeout: float = Field(default=120.0, ge=0.1, le=3600, allow_inf_nan=False)
    status: str = "pending"
    operation_id: str | None = None
    result: dict[str, Any] | None = None


class TaskSequence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    steps: list[SequenceStep] = Field(min_length=1, max_length=32)
    status: str = "running"
    active_index: int = 0
    reason: str = ""
    quiescent: bool = True
    updated_at: float = Field(default_factory=time.time)


class TaskSequenceExecutor:
    def __init__(
        self,
        path: Path,
        tools: Callable[[], dict[str, SkillInfo]],
        dispatch: Callable[[str, dict[str, Any], str], dict[str, Any]],
        cancel_operation: Callable[[str, str], bool],
        publish: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._path = path
        self._tools = tools
        self._dispatch = dispatch
        self._cancel_operation = cancel_operation
        self._publish = publish
        self._lock = RLock()
        self._wake = Event()
        self._sequence: TaskSequence | None = None
        self._runner: Thread | None = None
        self._future: Future[dict[str, Any]] | None = None
        if path.exists():
            if path.stat().st_size > MAX_STATE_BYTES:
                raise ValueError("Sequence state exceeds the storage limit")
            sequence = TaskSequence.model_validate_json(path.read_text())
            if sequence.status not in ("completed", "cancelled"):
                sequence.status = "paused"
                sequence.reason = "Restarted; explicit human review is required."
                for step in sequence.steps:
                    if step.status == "running":
                        step.status = "uncertain"
                        sequence.quiescent = False
            self._sequence = sequence
            self._save()

    def _save(self) -> None:
        assert self._sequence is not None
        self._sequence.updated_at = time.time()
        data = self._sequence.model_dump_json()
        if len(data.encode()) > MAX_STATE_BYTES:
            raise ValueError("Sequence state exceeds the storage limit")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        staging = self._path.with_suffix(".pending")
        with staging.open("w") as stream:
            os.chmod(staging, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        staging.replace(self._path)
        directory = os.open(self._path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        if self._publish is not None:
            self._publish(self.status())

    def status(self) -> dict[str, Any]:
        with self._lock:
            return (
                self._sequence.model_dump(mode="json")
                if self._sequence is not None
                else {"status": "idle"}
            )

    def _validate(self, steps: list[dict[str, Any]]) -> TaskSequence:
        if len(json.dumps(steps, allow_nan=False).encode()) > MAX_STATE_BYTES // 3:
            raise ValueError("Sequence plan exceeds the size limit")
        for item in steps:
            if set(item) - {"id", "tool", "arguments", "completion", "timeout"}:
                raise ValueError("Only plan fields may be supplied")
        sequence = TaskSequence(steps=[SequenceStep.model_validate(step) for step in steps])
        if len({step.id for step in sequence.steps}) != len(sequence.steps):
            raise ValueError("Step IDs must be unique")
        tools = self._tools()
        for step in sequence.steps:
            info = tools.get(step.tool)
            if info is None or step.tool in SEQUENCE_TOOL_NAMES:
                raise ValueError(f"Tool is not permitted in a sequence: {step.tool}")
            if info.completion != step.completion or info.lifecycle != "instant":
                raise ValueError(f"Tool has no supported bounded completion contract: {step.tool}")
            schema = json.loads(info.args_schema)
            schema["additionalProperties"] = False
            Draft202012Validator(schema).validate(step.arguments)
        return sequence

    def start(self, steps: list[dict[str, Any]]) -> dict[str, Any]:
        sequence = self._validate(steps)
        with self._lock:
            if self._sequence is not None and (
                self._sequence.status not in ("completed", "cancelled")
                or not self._sequence.quiescent
            ):
                raise ValueError("Cancel or finish the current sequence before starting another")
            if self._runner is not None and self._runner.is_alive():
                raise ValueError("Previous sequence worker is still stopping; retry shortly")
            self._sequence = sequence
            self._save()
            self._launch()
            return self.status()

    def _launch(self) -> None:
        if self._runner is not None and self._runner.is_alive():
            raise ValueError("Previous sequence worker is still stopping")
        self._wake.clear()
        self._runner = Thread(target=self._run, name="task-sequence", daemon=True)
        self._runner.start()

    def _run(self) -> None:
        while True:
            with self._lock:
                sequence = self._sequence
                if sequence is None or sequence.status != "running":
                    return
                if sequence.active_index == len(sequence.steps):
                    sequence.status = "completed"
                    self._save()
                    return
                step = sequence.steps[sequence.active_index]
                step.status = "running"
                step.operation_id = uuid.uuid4().hex
                sequence.quiescent = False
                self._save()  # Persist intent before any side effect.
                future: Future[dict[str, Any]] = Future()
                self._future = future
                Thread(target=self._invoke, args=(step, future, sequence.id), daemon=True).start()
            deadline = time.monotonic() + step.timeout
            while not future.done() and not self._wake.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.pause("Step timed out; review before retrying")
                    break
                self._wake.wait(min(0.05, remaining))
            with self._lock:
                if sequence.status != "running":
                    return
                if time.monotonic() >= deadline:
                    self.pause("Step timed out; review before continuing")
                    return
                try:
                    result = future.result()
                    # Never interpret legacy strings or an HTTP 200 as completion.
                    payload = result.get("structuredContent")
                    if not isinstance(payload, dict) or not isinstance(
                        payload.get("success"), bool
                    ):
                        raise ValueError("Tool returned no authoritative SkillResult")
                    step.result = self._bounded_result(payload)
                    if result.get("isError") or payload["success"] is not True:
                        raise ValueError(payload.get("message") or "Tool failed")
                except Exception as error:
                    step.status = "failed"
                    self.pause(str(error))
                    return
                step.status = "completed"
                sequence.quiescent = True
                sequence.active_index += 1
                self._save()

    def _invoke(self, step: SequenceStep, future: Future[dict[str, Any]], sequence_id: str) -> None:
        try:
            assert step.operation_id is not None
            try:
                self._validate(
                    [step.model_dump(include={"id", "tool", "arguments", "completion", "timeout"})]
                )
            except Exception as error:
                future.set_result(
                    {
                        "structuredContent": {
                            "success": False,
                            "message": f"Tool contract changed before dispatch: {error}"[:1000],
                            "error_code": "INVALID_STATE",
                        }
                    }
                )
            else:
                with self._lock:
                    permitted = (
                        self._sequence is not None
                        and self._sequence.id == sequence_id
                        and self._sequence.status == "running"
                        and step.status == "running"
                        and self._future is future
                    )
                if permitted:
                    future.set_result(
                        self._dispatch(step.tool, dict(step.arguments), step.operation_id)
                    )
                else:
                    future.set_result(
                        {
                            "structuredContent": {
                                "success": False,
                                "message": "Cancelled before tool dispatch",
                                "error_code": "INVALID_STATE",
                            }
                        }
                    )
        except BaseException as error:
            future.set_exception(error)
        finally:
            with self._lock:
                if (
                    self._sequence is not None
                    and self._sequence.id == sequence_id
                    and self._future is future
                ):
                    self._record_late_result(step, future)
                    self._sequence.quiescent = self._sequence.quiescent or self._verified_terminal(
                        future
                    )
                    self._save()

    def _record_late_result(self, step: SequenceStep, future: Future[dict[str, Any]]) -> None:
        if (
            self._sequence is not None
            and step.status == "uncertain"
            and self._verified_terminal(future)
        ):
            response = future.result()
            payload = response["structuredContent"]
            step.result = self._bounded_result(payload)
            if payload["success"] is True and not response.get("isError"):
                step.status = "completed"
                self._sequence.active_index += 1

    @staticmethod
    def _bounded_result(payload: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(payload, allow_nan=False).encode()) <= 4_000:
            return copy.deepcopy(payload)
        return {
            "success": payload["success"],
            "message": str(payload.get("message", ""))[:1000],
            "error_code": str(payload.get("error_code", ""))[:200],
            "metadata": {"result_truncated": True},
        }

    @staticmethod
    def _verified_terminal(future: Future[dict[str, Any]]) -> bool:
        if not future.done() or future.exception() is not None:
            return False
        payload = future.result().get("structuredContent")
        if not isinstance(payload, dict) or not isinstance(payload.get("success"), bool):
            return False
        try:
            json.dumps(payload, allow_nan=False)
        except (ValueError, TypeError):
            return False
        return True

    def pause(self, reason: str = "Paused by request") -> dict[str, Any]:
        return self._interrupt("paused", reason)

    def cancel(self) -> dict[str, Any]:
        return self._interrupt("cancelled", "Cancelled by request")

    def _interrupt(self, status: str, reason: str) -> dict[str, Any]:
        with self._lock:
            sequence = self._sequence
            if (
                sequence is None
                or sequence.status == "completed"
                or (sequence.status == "cancelled" and sequence.quiescent)
            ):
                return self.status()
            sequence.status = "cancelled" if sequence.status == "cancelled" else status
            sequence.reason = reason[:1000]
            self._wake.set()
            step = (
                sequence.steps[sequence.active_index]
                if sequence.active_index < len(sequence.steps)
                else None
            )
            if step is not None and step.status == "running":
                step.status = "uncertain"
                if self._future is not None:
                    self._record_late_result(step, self._future)
            self._save()
        if step is not None and step.operation_id is not None:
            try:
                stopped = self._cancel_operation(step.tool, step.operation_id)
            except Exception:
                logger.exception("Could not confirm task operation stopped", tool=step.tool)
                stopped = False
            with self._lock:
                # A synchronous call cannot be forcefully interrupted. Its worker
                # will confirm quiescence when it returns.
                sequence.quiescent = stopped or (
                    self._future is not None and self._verified_terminal(self._future)
                )
                self._save()
        return self.status()

    def resume(self, sequence_id: str, *, retry_failed: bool = False) -> dict[str, Any]:
        """Trusted human-confirmed RPC only; never export as an agent tool."""
        with self._lock:
            sequence = self._sequence
            if sequence is None or sequence.id != sequence_id or sequence.status != "paused":
                raise ValueError("No matching paused sequence")
            if not sequence.quiescent:
                step = sequence.steps[sequence.active_index]
                if step.operation_id is None or not self._cancel_operation(
                    step.tool, step.operation_id
                ):
                    raise ValueError(
                        "Active operation is not confirmed stopped; continuation blocked"
                    )
                sequence.quiescent = True
            if self._runner is not None and self._runner.is_alive():
                raise ValueError("Previous worker is still stopping; retry shortly")
            if sequence.active_index == len(sequence.steps):
                sequence.status = "completed"
                sequence.reason = ""
                self._save()
                return self.status()
            step = sequence.steps[sequence.active_index]
            if step.status in ("failed", "uncertain") and not retry_failed:
                raise ValueError(
                    "Explicit retry_failed is required for an uncertain or failed step"
                )
            # Revalidate permissions/contracts and arguments before replaying.
            self._validate(
                [
                    s.model_dump(include={"id", "tool", "arguments", "completion", "timeout"})
                    for s in sequence.steps[sequence.active_index :]
                ]
            )
            sequence.status = "running"
            sequence.reason = ""
            step.status = "pending"
            step.result = None
            self._save()
            self._launch()
            return self.status()

    def close(self) -> None:
        self.pause("Server stopped; human review is required")
        runner = self._runner
        if runner is not None:
            runner.join(timeout=2.0)
