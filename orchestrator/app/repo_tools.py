"""
Read-only repository tools (roadmap Faz I-2, docs/plan-i.md karar F).

Pure functions over a sandbox copy that the runner made without
secrets; the live code is never mounted into the orchestrator. main.py
asks the runner for the copy and calls these.
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

REPO_COPY = "repo"
MAX_LISTED_FILES = 200
MAX_READ_BYTES = 2 * 1024 * 1024
REPO_NAME = re.compile(r"^[a-z0-9-]{1,32}$")


class RepoError(ValueError):
    """The request cannot be answered; the message is for the model."""


def sandbox_repo(sandbox: Path) -> str | None:
    """Name of the repo copied into a sandbox, None when there is none."""
    try:
        return json.loads((sandbox / "repo.json").read_text(encoding="utf-8"))["repo"]
    except (OSError, ValueError, KeyError):
        return None


def check_repo_name(repo) -> str:
    if not isinstance(repo, str) or not REPO_NAME.match(repo):
        raise RepoError("repo must be a repository name such as savas-ai.")
    return repo


def list_files(root: Path, pattern: str | None) -> dict:
    """Files of the copy matching a glob (default: all), at most MAX_LISTED_FILES."""
    pattern = (pattern or "*").strip() or "*"
    matched = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
        and fnmatch.fnmatch(str(path.relative_to(root)), pattern)
    )
    result = {"pattern": pattern, "count": len(matched), "files": matched[:MAX_LISTED_FILES]}
    if len(matched) > MAX_LISTED_FILES:
        result["note"] = f"İlk {MAX_LISTED_FILES} dosya; daha dar bir pattern ver."
    return result


def read_file(root: Path, path) -> dict:
    """A text file of the copy; paths outside it, links and binaries are refused."""
    if not isinstance(path, str) or not path.strip():
        raise RepoError("path is required.")
    candidate = root / path.strip().lstrip("/")
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        raise RepoError(f"No such file in the repository: {path}") from None
    if candidate.is_symlink() or not resolved.is_file():
        raise RepoError(f"Not a regular file: {path}")
    if resolved.stat().st_size > MAX_READ_BYTES:
        raise RepoError(f"File is larger than {MAX_READ_BYTES // (1024 * 1024)} MB: {path}")
    try:
        content = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise RepoError(f"Not a text file: {path}") from None
    return {"path": str(relative), "content": content}
