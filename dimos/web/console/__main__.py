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

"""Run the local console independently of the robot stack."""

import argparse
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
import signal
from types import FrameType

from fastapi import FastAPI
import uvicorn

from dimos.web.console.module import RobotConsoleModule
from dimos.web.console.settings import ConsoleRuntime, ConsoleSettings


def _on_terminal_close(_signal: int, _frame: FrameType | None) -> None:
    signal.raise_signal(signal.SIGTERM)


def main() -> None:
    parser = argparse.ArgumentParser(description="SEDAN GROUP local robot console")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    args = parser.parse_args()
    runtime = ConsoleRuntime(args.project_dir, console_port=args.port)
    module = RobotConsoleModule(
        port=args.port,
        mcp_port=runtime.settings.mcp_port,
        rerun_web_port=runtime.settings.rerun_web_port,
        rerun_grpc_port=runtime.settings.rerun_grpc_port,
    )
    module.runtime = runtime
    runtime.on_event = module._emit

    def apply_settings(settings: ConsoleSettings) -> None:
        if args.port in {settings.mcp_port, settings.rerun_web_port, settings.rerun_grpc_port}:
            raise ValueError("Console, MCP and Rerun must use different ports")
        module.config.mcp_port = settings.mcp_port
        module.config.rerun_web_port = settings.rerun_web_port
        module.config.rerun_grpc_port = settings.rerun_grpc_port

    runtime.on_settings = apply_settings
    apply_settings(runtime.settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        module._console_loop = asyncio.get_running_loop()
        try:
            module._setup_agent_streams()
            yield
        finally:
            try:
                await asyncio.to_thread(runtime.shutdown)
            finally:
                try:
                    module._teardown_agent_streams()
                finally:
                    module._close_module()
                    module._console_loop = None

    app = module._build_app()
    app.router.lifespan_context = lifespan
    previous_hup = signal.signal(signal.SIGHUP, _on_terminal_close)
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        signal.signal(signal.SIGHUP, previous_hup)
        runtime.shutdown()


if __name__ == "__main__":
    main()
