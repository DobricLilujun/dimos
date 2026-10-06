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

"""Process-wide logging setup for the standalone web console.

``python -m dimos.web.console`` runs outside ``dimos run``, so it never gets the
per-run log directory that the CLI installs.  This module gives the console the
same treatment: a discoverable, timestamped log directory that the console's own
structured logs and the captured robot-stack output both land in, plus a handler
for uncaught exceptions and an optional debug level.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time

from dimos.constants import LOG_DIR
from dimos.utils.logging_config import (
    set_log_level,
    set_run_log_dir,
    setup_exception_handler,
)


def _console_log_dir() -> Path:
    """Create a timestamped log directory for this console process.

    Falls back to a temp directory if the configured ``LOG_DIR`` is not writable
    so logging never blocks startup.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    configured = os.environ.get("DIMOS_LOG_DIR")
    base = Path(configured) if configured else LOG_DIR
    candidates = (base / "web-console" / stamp, Path(tempfile.gettempdir()) / "dimos" / "logs" / "web-console" / stamp)
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError:
            continue
    # Last resort: return the first candidate even if it could not be created;
    # the RotatingFileHandler / stack-log openers handle the missing path.
    return candidates[0]


def setup_console_logging(*, debug: bool = False) -> Path:
    """Configure process-wide logging for the web console.

    - Creates a dedicated, discoverable log directory.
    - Routes the console's structured (``main.jsonl``) logs there.
    - Installs a handler for uncaught exceptions (full tracebacks).
    - Enables DEBUG logging across the console and its child stack when asked;
      the child inherits ``DIMOS_LOG_LEVEL`` from the environment.

    Returns the created log directory.
    """
    if debug:
        os.environ["DIMOS_LOG_LEVEL"] = "DEBUG"
    log_dir = _console_log_dir()
    set_run_log_dir(log_dir)
    setup_exception_handler()
    set_log_level()
    return log_dir
