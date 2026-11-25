from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
import re

from context_engine import RETRIEVED_NOTE, build_context


class WorkerError(Exception):
    pass


class WorkerManager:
    """
    Persistent worker identity + daily task memory manager.

    Worker identity:
        SAVAS-AI/workers/<worker>/<worker>.md

    Daily task memory:
        SAVAS-AI/workers/<worker>/daily/YYYY-MM-DD-task-<task_id>.md
    """

    WORKER_ID_RE = re.compile(r"^[a-z0-9_-]+$")
    TASK_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

    def __init__(
        self,
        root: str | Path,
        retention_days: int = 4,
    ):
        self.root = Path(root).resolve()
        self.workers_root = self.root / "workers"
        self.shared_root = self.root / "shared"
        self.retention_days = retention_days

        self.workers_root.mkdir(parents=True, exist_ok=True)
        self.shared_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_worker_id(self, worker_id: str) -> str:
        if not worker_id or not self.WORKER_ID_RE.fullmatch(worker_id):
            raise WorkerError(f"Invalid worker id: {worker_id!r}")
        return worker_id

    def validate_task_id(self, task_id: str) -> str:
        if not task_id or not self.TASK_ID_RE.fullmatch(task_id):
            raise WorkerError(f"Invalid task id: {task_id!r}")
        return task_id

    # ------------------------------------------------------------------
    # Worker paths
    # ------------------------------------------------------------------

    def worker_root(self, worker_id: str) -> Path:
        worker_id = self.validate_worker_id(worker_id)
        return self.workers_root / worker_id

    def worker_memory_path(self, worker_id: str) -> Path:
        worker_id = self.validate_worker_id(worker_id)
        return self.worker_root(worker_id) / f"{worker_id}.md"

    def worker_daily_root(self, worker_id: str) -> Path:
        worker_id = self.validate_worker_id(worker_id)
        return self.worker_root(worker_id) / "daily"

    def daily_task_path(
        self,
        worker_id: str,
        task_id: str,
        task_date: date | None = None,
    ) -> Path:
        worker_id = self.validate_worker_id(worker_id)
        task_id = self.validate_task_id(task_id)

        if task_date is None:
            task_date = date.today()

        return (
            self.worker_daily_root(worker_id)
            / f"{task_date.isoformat()}-task-{task_id}.md"
        )

    # ------------------------------------------------------------------
    # Worker discovery
    # ------------------------------------------------------------------

    def list_workers(self) -> list[str]:
        if not self.workers_root.exists():
            return []

        return sorted(
            item.name
            for item in self.workers_root.iterdir()
            if item.is_dir() and self.WORKER_ID_RE.fullmatch(item.name)
        )

    def worker_exists(self, worker_id: str) -> bool:
        return self.worker_root(worker_id).is_dir()

    # ------------------------------------------------------------------
    # Persistent worker memory
    # ------------------------------------------------------------------

    def read_worker_memory(self, worker_id: str) -> str:
        path = self.worker_memory_path(worker_id)

        if not path.exists():
            raise WorkerError(
                f"Persistent worker memory does not exist: {path}"
            )

        return path.read_text(encoding="utf-8")

    def write_worker_memory(self, worker_id: str, content: str) -> Path:
        """
        Worker can only write its own persistent memory.
        The caller is responsible for ensuring worker identity.
        """
        path = self.worker_memory_path(worker_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    # ------------------------------------------------------------------
    # Daily task memory
    # ------------------------------------------------------------------

    def create_daily_task(
        self,
        worker_id: str,
        task_id: str,
        task: str,
    ) -> Path:
        path = self.daily_task_path(worker_id, task_id)

        path.parent.mkdir(parents=True, exist_ok=True)

        if not path.exists():
            content = f"""# Task {task_id}

## Worker

{worker_id}

## Date

{date.today().isoformat()}

## Task

{task}

## Status

In Progress

## Work Log

## Findings

## Decisions

## Current State

## Next Step

"""
            path.write_text(content, encoding="utf-8")

        return path

    def read_daily_task(
        self,
        worker_id: str,
        task_id: str,
        task_date: date | None = None,
    ) -> str:
        path = self.daily_task_path(worker_id, task_id, task_date)

        if not path.exists():
            raise WorkerError(f"Daily task memory does not exist: {path}")

        return path.read_text(encoding="utf-8")

    def write_daily_task(
        self,
        worker_id: str,
        task_id: str,
        content: str,
        task_date: date | None = None,
    ) -> Path:
        path = self.daily_task_path(worker_id, task_id, task_date)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

        return path

    def append_daily_task(
        self,
        worker_id: str,
        task_id: str,
        content: str,
        task_date: date | None = None,
    ) -> Path:
        path = self.daily_task_path(worker_id, task_id, task_date)

        path.parent.mkdir(parents=True, exist_ok=True)

        with path.open("a", encoding="utf-8") as f:
            f.write("\n")
            f.write(content.rstrip())
            f.write("\n")

        return path

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------

    def build_worker_context(
        self,
        worker_id: str,
        task_id: str | None = None,
        task_date: date | None = None,
        include_task: bool = True,
        budget_chars: int | None = None,
        task_chars: int = 0,
        retrieved: list[str] = (),
        retrieved_chars: int = 0,
        retrieved_note: str = RETRIEVED_NOTE,
    ) -> str:
        """
        Build the persistent + current-task context that is injected
        into the worker's LLM conversation.

        include_task=False drops the daily file's "## Task" body for
        callers that already send the task as the user message.

        budget_chars: build the context within that many characters
        (context_engine.build_context); task_chars is the size of the
        task text the caller sends itself. Without a budget both files
        are included whole. retrieved / retrieved_chars: memory lines
        found for the task (budgeted context only).
        """

        worker_memory = self.read_worker_memory(worker_id)

        if budget_chars is not None:
            daily_memory = None

            if task_id is not None:
                daily_path = self.daily_task_path(
                    worker_id,
                    task_id,
                    task_date,
                )

                if daily_path.exists():
                    daily_memory = daily_path.read_text(
                        encoding="utf-8"
                    )

            return build_context(
                worker_memory,
                daily_memory,
                budget_chars,
                task_chars,
                include_task,
                retrieved,
                retrieved_chars,
                retrieved_note,
            )

        sections = [
            "# WORKER IDENTITY",
            "",
            worker_memory,
        ]

        if task_id is not None:
            daily_path = self.daily_task_path(
                worker_id,
                task_id,
                task_date,
            )

            if daily_path.exists():
                daily_memory = daily_path.read_text(
                    encoding="utf-8"
                )

                if not include_task:
                    daily_memory = re.sub(
                        r"(?m)^## Task\s*$[\s\S]*?(?=^## |\Z)",
                        "## Task\n\n(Görev metni kullanıcı mesajında.)\n\n",
                        daily_memory,
                        count=1,
                    )

                sections.extend(
                    [
                        "",
                        "# CURRENT DAILY WORK MEMORY",
                        "",
                        daily_memory,
                    ]
                )

        return "\n".join(sections)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup_old_daily_files(self) -> list[str]:
        """
        Delete .md files older than retention_days from worker daily
        directories.

        Persistent worker files are never touched.
        """

        cutoff = datetime.now() - timedelta(days=self.retention_days)
        deleted: list[str] = []

        if not self.workers_root.exists():
            return deleted

        for worker_dir in self.workers_root.iterdir():
            if not worker_dir.is_dir():
                continue

            daily_dir = worker_dir / "daily"

            if not daily_dir.is_dir():
                continue

            for path in daily_dir.rglob("*.md"):
                try:
                    modified = datetime.fromtimestamp(path.stat().st_mtime)
                except FileNotFoundError:
                    continue

                if modified < cutoff:
                    try:
                        path.unlink()
                        deleted.append(str(path))
                    except FileNotFoundError:
                        pass

            # Remove empty directories below daily/
            for directory in sorted(
                daily_dir.rglob("*"),
                key=lambda p: len(p.parts),
                reverse=True,
            ):
                if directory.is_dir():
                    try:
                        directory.rmdir()
                    except OSError:
                        pass

        return deleted

    def update_daily_section(
        self,
        worker_id: str,
        task_id: str,
        section: str,
        content: str,
        task_date: date | None = None,
    ) -> Path:
        """
        Update a single markdown section in a daily task file.

        If the section does not exist, it is appended.
        Existing content for that section is replaced.
        """

        path = self.daily_task_path(
            worker_id,
            task_id,
            task_date,
        )

        if not path.exists():
            raise WorkerError(
                f"Daily task memory does not exist: {path}"
            )

        text = path.read_text(encoding="utf-8")

        section_pattern = re.compile(
            rf"(?m)^## {re.escape(section)}\s*$"
            rf"[\s\S]*?"
            rf"(?=^## |\Z)"
        )

        replacement = (
            f"## {section}\n\n"
            f"{content.rstrip()}\n\n"
        )

        if section_pattern.search(text):
            # A function, so backslashes in the content (code,
            # regexes) are kept as written, not read as escapes.
            text = section_pattern.sub(
                lambda match: replacement,
                text,
                count=1,
            )
        else:
            if not text.endswith("\n"):
                text += "\n"

            text += f"\n{replacement}"

        path.write_text(
            text,
            encoding="utf-8",
        )

        return path

    def append_daily_section(
        self,
        worker_id: str,
        task_id: str,
        section: str,
        content: str,
        task_date: date | None = None,
    ) -> Path:
        """
        Append content to an existing markdown section.

        The section heading is preserved and content is inserted
        before the next section heading.
        """

        path = self.daily_task_path(
            worker_id,
            task_id,
            task_date,
        )

        if not path.exists():
            raise WorkerError(
                f"Daily task memory does not exist: {path}"
            )

        text = path.read_text(encoding="utf-8")

        section_pattern = re.compile(
            rf"(?m)^## {re.escape(section)}\s*$"
            rf"([\s\S]*?)"
            rf"(?=^## |\Z)"
        )

        match = section_pattern.search(text)

        if match:
            existing = match.group(1).rstrip()

            addition = content.rstrip()

            if existing:
                new_section_content = (
                    f"{existing}\n\n{addition}\n\n"
                )
            else:
                new_section_content = (
                    f"\n{addition}\n\n"
                )

            replacement = (
                f"## {section}\n"
                f"{new_section_content}"
            )

            text = (
                text[:match.start()]
                + replacement
                + text[match.end():]
            )
        else:
            if not text.endswith("\n"):
                text += "\n"

            text += (
                f"\n## {section}\n\n"
                f"{content.rstrip()}\n\n"
            )

        path.write_text(
            text,
            encoding="utf-8",
        )

        return path
