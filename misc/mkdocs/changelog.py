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

"""Generate a timestamped changelog ("Updates") page from the git history.

The user asked that every update be recorded with its time at the end of the
site. This hook builds a changelog page from the commit log at build time so
each deploy reflects the latest changes, and appends it to the navigation so it
sits at the end of the site (a small blog of what shipped and when).

It is safe to run without a git checkout: if the history cannot be read the
changelog is simply omitted.
"""

import re
import subprocess

from mkdocs.structure.files import File

CHANGELOG_URL = "sedan/updates.md"
MAX_COMMITS = 60


def _clean(message: str) -> str:
    # Collapse a multi-line commit body to the first line; trim the type prefix.
    first = message.splitlines()[0] if message else ""
    first = re.sub(r"^merge\s+", "merge ", first, flags=re.I)
    return first.strip()


def _git_log() -> list[dict[str, str]]:
    """Return recent commits as {date, hash, message}; [] on any failure."""
    try:
        proc = subprocess.run(
            ["git", "log", f"-n{MAX_COMMITS}", "--format=%ad|%h|%s",
             "--date=format:%Y-%m-%d"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out: list[dict[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        date, short, message = parts
        out.append({"date": date.strip(), "hash": short.strip(), "message": message.strip()})
    return out


def _render(commits: list[dict[str, str]]) -> str:
    by_date: dict[str, list[dict[str, str]]] = {}
    for c in commits:
        by_date.setdefault(c["date"], []).append(c)

    body = ["# Updates", ""]
    body.append(
        "A timestamped record of what shipped and when. Each entry below is a "
        "commit on `learning-branch`; the date is its commit date. Newest first."
    )
    body.append("")
    for date in sorted(by_date, reverse=True):
        body.append(f"## {date}")
        body.append("")
        for c in by_date[date]:
            message = _clean(c["message"])
            if not message:
                continue
            body.append(f"- `{c['hash']}` — {message}")
        body.append("")
    return "\n".join(body)


def on_config(config) -> None:
    global _COMMITS
    _COMMITS = _git_log()


def on_files(files, config) -> None:
    if not _COMMITS:
        return
    content = _render(_COMMITS)
    files.append(File.generated(config, CHANGELOG_URL, content=content))
