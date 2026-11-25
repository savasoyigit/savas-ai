"""
Model registry (roadmap C-3).

Loads and validates config/models.json. All problems are collected and
raised together as one RegistryError so a bad edit is easy to fix.
select() (C-4) picks a model deterministically; it does not touch the LLM
call path itself (integration is C-5). CircuitBreaker (C-6) keeps the
in-memory health state that main.py passes to select() as `unavailable`.
Local models (C-8.2) are always the last candidate, unless the daily
cloud budget is used up.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Collection
from datetime import date, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import time

DEFAULT_REGISTRY_PATH = Path(
    os.getenv(
        "MODEL_REGISTRY_PATH",
        Path(__file__).resolve().parent / "config" / "models.json",
    )
)

CAPABILITIES = frozenset(
    {"tool_calling", "reasoning", "coding", "vision", "long_context", "cheap"}
)
LOCAL = "local"
LOCATIONS = frozenset({"cloud", LOCAL})
ORCHESTRATOR_AGENT = "ares"
ORCHESTRATE_TASK = "orchestrate"
GENERAL_TASK = "general"

TOP_FIELDS = {"models", "task_types", "workers", "default_model"}
OPTIONAL_TOP_FIELDS = {"local_switch_percent"}
# Projected daily cloud usage (percent) above which calls move to a local model.
DEFAULT_LOCAL_SWITCH_PERCENT = 100
MODEL_FIELDS = {
    "id", "provider", "location", "capabilities", "enabled",
    "max_output", "context", "fallback", "verified", "notes",
}
TASK_TYPE_FIELDS = {"requires", "prefer"}
WORKER_FIELDS = {"default", "allowed"}


class RegistryError(Exception):
    pass


@dataclass(frozen=True)
class ModelEntry:
    id: str
    provider: str
    location: str
    capabilities: frozenset[str]
    enabled: bool
    max_output: int
    context: int | None
    fallback: str | None
    verified: date | None
    notes: str


@dataclass(frozen=True)
class TaskType:
    requires: tuple[str, ...]
    prefer: tuple[str, ...]


@dataclass(frozen=True)
class WorkerRoute:
    default: str
    allowed: tuple[str, ...]


@dataclass(frozen=True)
class Registry:
    models: tuple[ModelEntry, ...]
    task_types: dict[str, TaskType]
    workers: dict[str, WorkerRoute]
    default_model: str
    local_switch_percent: int = DEFAULT_LOCAL_SWITCH_PERCENT

    def model(self, model_id: str) -> ModelEntry:
        for entry in self.models:
            if entry.id == model_id:
                return entry
        raise KeyError(model_id)


def _check_fields(
    where: str, obj, allowed: set[str], errors: list[str], optional: Collection[str] = frozenset()
) -> bool:
    if not isinstance(obj, dict):
        errors.append(f"{where}: nesne olmalı")
        return False
    unknown = sorted(set(obj) - allowed - set(optional))
    missing = sorted(allowed - set(obj))
    if unknown:
        errors.append(f"{where}: bilinmeyen alan {unknown}")
    if missing:
        errors.append(f"{where}: eksik alan {missing}")
    return not missing


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _str_list(where: str, value, errors: list[str]) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors.append(f"{where}: metin listesi olmalı")
        return []
    return value


def _parse_model(where: str, raw: dict, errors: list[str]) -> ModelEntry | None:
    if not _check_fields(where, raw, MODEL_FIELDS, errors):
        return None

    if not isinstance(raw["id"], str) or not raw["id"]:
        errors.append(f"{where}.id: boş olmayan metin olmalı")
    if not isinstance(raw["provider"], str):
        errors.append(f"{where}.provider: metin olmalı")
    if raw["location"] not in LOCATIONS:
        errors.append(f"{where}.location: {sorted(LOCATIONS)} içinden olmalı, {raw['location']!r} geldi")

    capabilities = _str_list(f"{where}.capabilities", raw["capabilities"], errors)
    unknown_caps = sorted(set(capabilities) - CAPABILITIES)
    if unknown_caps:
        errors.append(f"{where}.capabilities: bilinmeyen capability {unknown_caps}; izinli: {sorted(CAPABILITIES)}")

    if not isinstance(raw["enabled"], bool):
        errors.append(f"{where}.enabled: true/false olmalı")
    if not _is_int(raw["max_output"]) or raw["max_output"] <= 0:
        errors.append(f"{where}.max_output: pozitif tam sayı olmalı")
    if raw["context"] is not None and (not _is_int(raw["context"]) or raw["context"] <= 0):
        errors.append(f"{where}.context: null veya pozitif tam sayı olmalı")
    if raw["location"] == LOCAL and raw["context"] is None:
        # Requests larger than this are not routed to the model.
        errors.append(f"{where}.context: local model için zorunlu")
    if raw["fallback"] is not None and not isinstance(raw["fallback"], str):
        errors.append(f"{where}.fallback: null veya model id olmalı")
    if not isinstance(raw["notes"], str):
        errors.append(f"{where}.notes: metin olmalı")

    verified = None
    if raw["verified"] is not None:
        try:
            verified = date.fromisoformat(raw["verified"])
        except (TypeError, ValueError):
            errors.append(f"{where}.verified: null veya YYYY-MM-DD olmalı, {raw['verified']!r} geldi")

    return ModelEntry(
        id=raw["id"],
        provider=raw["provider"],
        location=raw["location"],
        capabilities=frozenset(capabilities),
        enabled=raw["enabled"],
        max_output=raw["max_output"],
        context=raw["context"],
        fallback=raw["fallback"],
        verified=verified,
        notes=raw["notes"],
    )


def parse_registry(data) -> Registry:
    errors: list[str] = []
    if not _check_fields("kök", data, TOP_FIELDS, errors, OPTIONAL_TOP_FIELDS):
        raise RegistryError("\n".join(errors))

    # models
    models: list[ModelEntry] = []
    if not isinstance(data["models"], list) or not data["models"]:
        errors.append("models: boş olmayan liste olmalı")
    else:
        for index, raw in enumerate(data["models"]):
            entry = _parse_model(f"models[{index}]", raw, errors)
            if entry is not None:
                models.append(entry)

    by_id: dict[str, ModelEntry] = {}
    for entry in models:
        if entry.id in by_id:
            errors.append(f"models: tekrar eden id {entry.id!r}")
        by_id[entry.id] = entry

    for entry in models:
        if entry.fallback is None:
            continue
        if entry.fallback == entry.id:
            errors.append(f"{entry.id}.fallback: model kendisini yedek gösteremez")
        elif entry.fallback not in by_id:
            errors.append(f"{entry.id}.fallback: {entry.fallback!r} registry'de yok")
        elif not by_id[entry.fallback].enabled:
            errors.append(f"{entry.id}.fallback: {entry.fallback!r} kapalı (enabled=false)")

    # task_types
    task_types: dict[str, TaskType] = {}
    if not isinstance(data["task_types"], dict):
        errors.append("task_types: nesne olmalı")
    else:
        for name, raw in data["task_types"].items():
            where = f"task_types.{name}"
            if not _check_fields(where, raw, TASK_TYPE_FIELDS, errors):
                continue
            requires = _str_list(f"{where}.requires", raw["requires"], errors)
            prefer = _str_list(f"{where}.prefer", raw["prefer"], errors)
            unknown_caps = sorted(set(requires) - CAPABILITIES)
            if unknown_caps:
                errors.append(f"{where}.requires: bilinmeyen capability {unknown_caps}")
            missing = [m for m in prefer if m not in by_id]
            if missing:
                errors.append(f"{where}.prefer: registry'de olmayan model {missing}")
            task_types[name] = TaskType(tuple(requires), tuple(prefer))

    orchestrate = task_types.get(ORCHESTRATE_TASK)
    if orchestrate is None or "tool_calling" not in orchestrate.requires:
        errors.append(f"task_types.{ORCHESTRATE_TASK}: tanımlı olmalı ve requires içinde tool_calling bulunmalı")

    # workers
    workers: dict[str, WorkerRoute] = {}
    if not isinstance(data["workers"], dict):
        errors.append("workers: nesne olmalı")
    else:
        for agent, raw in data["workers"].items():
            where = f"workers.{agent}"
            if not _check_fields(where, raw, WORKER_FIELDS, errors):
                continue
            allowed = _str_list(f"{where}.allowed", raw["allowed"], errors)
            unknown = [t for t in allowed if t not in task_types]
            if unknown:
                errors.append(f"{where}.allowed: tanımsız task_type {unknown}")
            if raw["default"] not in allowed:
                errors.append(f"{where}.default: {raw['default']!r} allowed listesinde olmalı")
            workers[agent] = WorkerRoute(raw["default"], tuple(allowed))

    ares = workers.get(ORCHESTRATOR_AGENT)
    if ares is None or ares.default != ORCHESTRATE_TASK:
        errors.append(f"workers.{ORCHESTRATOR_AGENT}: tanımlı olmalı ve default={ORCHESTRATE_TASK!r} olmalı")

    default_model = data["default_model"]
    if default_model not in by_id:
        errors.append(f"default_model: {default_model!r} registry'de yok")
    elif not by_id[default_model].enabled:
        errors.append(f"default_model: {default_model!r} kapalı (enabled=false)")

    local_switch_percent = data.get("local_switch_percent", DEFAULT_LOCAL_SWITCH_PERCENT)
    if not _is_int(local_switch_percent) or not 1 <= local_switch_percent <= 100:
        errors.append("local_switch_percent: 1-100 arası tam sayı olmalı")

    if errors:
        raise RegistryError("Model registry geçersiz:\n- " + "\n- ".join(errors))

    return Registry(tuple(models), task_types, workers, default_model, local_switch_percent)


def load_registry(path: str | Path = DEFAULT_REGISTRY_PATH) -> Registry:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RegistryError(f"Model registry bulunamadı: {path}") from None

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RegistryError(
            f"{path}: JSON hatası satır {exc.lineno}, sütun {exc.colno}: {exc.msg}"
        ) from None

    try:
        return parse_registry(data)
    except RegistryError as exc:
        raise RegistryError(f"{path}: {exc}") from None


# ----------------------------------------------------------------------
# Selection (C-4)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Selection:
    model: str
    reason: str
    fallbacks: tuple[str, ...]
    # ids among model + fallbacks that are served by the local LLM
    local: frozenset[str] = frozenset()


_cache: dict[Path, tuple[float, Registry]] = {}


def get_registry(path: str | Path = DEFAULT_REGISTRY_PATH) -> Registry:
    """Load the registry, reusing the parsed copy while the file is unchanged."""
    path = Path(path)
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        raise RegistryError(f"Model registry bulunamadı: {path}") from None
    cached = _cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    registry = load_registry(path)
    _cache[path] = (mtime, registry)
    return registry


def _usable(
    entry: ModelEntry,
    requires: tuple[str, ...],
    unavailable: Collection[str],
    needed_tokens: int | None = None,
    allow_local: bool = True,
) -> bool:
    return (
        entry.enabled
        and entry.verified is not None
        and entry.id not in unavailable
        and set(requires) <= entry.capabilities
        and (allow_local or entry.location != LOCAL)
        # A request that does not fit would be cut off silently.
        and (needed_tokens is None or entry.context is None or needed_tokens <= entry.context)
    )


def select(
    agent: str,
    task_type: str | None = None,
    *,
    registry: Registry | None = None,
    registry_path: str | Path = DEFAULT_REGISTRY_PATH,
    unavailable: Collection[str] = frozenset(),
    env_model: str | None = None,
    needed_tokens: int | None = None,
    allow_local: bool = True,
    cloud_budget_percent: float | None = None,
) -> Selection:
    """
    Pick the model for one LLM call.

    Order: task type (explicit if allowed, else the agent default) ->
    usable candidates (enabled, verified, capabilities, not unavailable)
    sorted by `prefer` position then id -> first one, plus its registry
    fallback if that is usable too. No candidate -> default_model if
    usable (capabilities relaxed except tool_calling) -> MODEL env as
    last resort. No randomness.

    Local models (C-8.2) sort after every cloud candidate and the first
    one is appended as the last fallback. needed_tokens (prompt estimate
    + completion reserve) excludes models whose `context` is smaller.
    cloud_budget_percent above the registry's local_switch_percent picks
    the local candidate directly; without one the cloud selection stands
    and the budget check blocks the call as before.
    """
    if env_model is None:
        env_model = os.getenv("MODEL", "gh/gpt-4.1")

    if registry is None:
        try:
            registry = get_registry(registry_path)
        except RegistryError as exc:
            first_line = str(exc).splitlines()[0]
            return Selection(env_model, f"config hatası → env MODEL ({first_line})", ())

    notes = []
    route = registry.workers.get(agent)
    if route is None:
        used = GENERAL_TASK if GENERAL_TASK in registry.task_types else None
        notes.append("agent registry'de yok")
    elif task_type is None:
        used = route.default
    elif task_type in route.allowed:
        used = task_type
    else:
        used = route.default
        notes.append(f"task_type {task_type!r} izinli değil → varsayılan")

    prefix = f"{agent}/{used}:"
    suffix = f" ({'; '.join(notes)})" if notes else ""

    spec = registry.task_types.get(used) if used else None
    requires = spec.requires if spec else ()
    prefer = spec.prefer if spec else ()

    def usable(entry: ModelEntry, caps: tuple[str, ...] = requires) -> bool:
        return _usable(entry, caps, unavailable, needed_tokens, allow_local)

    candidates = sorted(
        (m for m in registry.models if usable(m)),
        key=lambda m: (
            m.location == LOCAL,
            prefer.index(m.id) if m.id in prefer else len(prefer),
            m.id,
        ),
    )
    local = next((m for m in candidates if m.location == LOCAL), None)

    if (
        local is not None
        and cloud_budget_percent is not None
        and cloud_budget_percent > registry.local_switch_percent
    ):
        reason = f"{prefix} budget: local switch ({cloud_budget_percent:.0f}%){suffix}"
        return Selection(local.id, reason, (), frozenset({local.id}))

    if candidates:
        chosen = candidates[0]
        if chosen is local:
            how = "bulut adayı yok → local"
        elif chosen.id in prefer:
            how = f"prefer[{prefer.index(chosen.id)}]"
        else:
            how = "alfabetik"
        fallbacks: list[str] = []
        if chosen.fallback:
            backup = registry.model(chosen.fallback)
            if usable(backup):
                fallbacks.append(backup.id)
        if local is not None and chosen is not local and local.id not in fallbacks:
            fallbacks.append(local.id)
        served_locally = frozenset(
            m for m in (chosen.id, *fallbacks) if registry.model(m).location == LOCAL
        )
        return Selection(chosen.id, f"{prefix} {how}{suffix}", tuple(fallbacks), served_locally)

    # default_model relaxes capability requirements (otherwise it would
    # already be a candidate), except tool_calling: Ares cannot work without it.
    default = registry.model(registry.default_model)
    hard = tuple(r for r in requires if r == "tool_calling")
    if usable(default, hard):
        return Selection(default.id, f"{prefix} aday yok → default_model{suffix}", ())

    return Selection(
        env_model,
        f"{prefix} aday yok, default_model kullanılamaz → env MODEL{suffix}",
        (),
    )


# ----------------------------------------------------------------------
# Fallback + health (C-6)
# ----------------------------------------------------------------------

# Failure kinds. Every kind except FATAL may move a call to its fallback.
LIMIT = "limit"          # provider limit / quota: disable at once
TRANSIENT = "transient"  # 466, 5xx, timeout, connection error
ACCESS = "access"        # 403: needs the user, but work goes on via fallback
FATAL = "fatal"          # request / config error: must stay visible

FAILURE_THRESHOLD = 2
COOLDOWN_SECONDS = 600
MAX_COOLDOWN_SECONDS = 24 * 3600
LIMIT_MARKERS = ("quota", "rate limit", "rate_limit", "too many requests")


def classify_failure(status: int | None, body: str = "") -> str:
    """Kind of a failed LLM call. status None = no HTTP response at all."""
    if status is None:
        return TRANSIENT
    if status == 429:
        return LIMIT
    if status in (400, 401):
        return FATAL
    text = (body or "").lower()
    if any(marker in text for marker in LIMIT_MARKERS):
        return LIMIT
    if status == 466 or status >= 500:
        return TRANSIENT
    if status == 403:
        return ACCESS
    return FATAL


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """Retry-After header (seconds or HTTP date) as seconds from now."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value.strip())
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = when.timestamp() - (time.time() if now is None else now)
    return seconds if seconds > 0 else None


class CircuitBreaker:
    """
    Lazy, in-memory model health. Valid for a single uvicorn process;
    a restart forgets everything.

    A limit failure disables the model at once (Retry-After if the
    provider sent one, else COOLDOWN_SECONDS). FAILURE_THRESHOLD
    consecutive transient / access failures disable it for
    COOLDOWN_SECONDS.
    There is no background task: callers ask unavailable() / expire().
    """

    def __init__(self, clock=time.time):
        self.clock = clock
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    def _is_open(self, model: str) -> bool:
        return self._open_until.get(model, 0) > self.clock()

    def record_failure(self, model: str, kind: str, retry_after: float | None = None) -> float | None:
        """Returns the time the model is disabled until, if this failure disabled it."""
        if kind == LIMIT:
            seconds = min(retry_after, MAX_COOLDOWN_SECONDS) if retry_after else COOLDOWN_SECONDS
        elif kind in (TRANSIENT, ACCESS):
            self._failures[model] = self._failures.get(model, 0) + 1
            if self._failures[model] < FAILURE_THRESHOLD:
                return None
            seconds = COOLDOWN_SECONDS
        else:
            return None

        self._failures.pop(model, None)
        was_open = self._is_open(model)
        until = self.clock() + seconds
        # A last-resort call can hit a model that is already disabled.
        self._open_until[model] = max(until, self._open_until[model]) if was_open else until
        return None if was_open else until

    def record_success(self, model: str) -> bool:
        """Returns True if this success re-enabled a disabled model."""
        self._failures.pop(model, None)
        return self._open_until.pop(model, None) is not None

    def expire(self) -> list[str]:
        """Models whose cool-down ended since the last call; each reported once."""
        now = self.clock()
        ended = sorted(m for m, until in self._open_until.items() if until <= now)
        for model in ended:
            del self._open_until[model]
        return ended

    def unavailable(self) -> frozenset[str]:
        return frozenset(m for m in self._open_until if self._is_open(m))
