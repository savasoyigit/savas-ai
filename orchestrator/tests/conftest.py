import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from budget_manager import BudgetManager  # noqa: E402
from token_manager import TokenManager  # noqa: E402


@pytest.fixture
def managers(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_DAILY_TOKEN_BUDGET", "5000")
    monkeypatch.setenv("ARES_DAILY_TOKEN_BUDGET", "20000")
    monkeypatch.setenv("SYSTEM_DAILY_TOKEN_BUDGET", "50000")
    monkeypatch.setenv("TASK_TOKEN_BUDGET", "10000")
    monkeypatch.setenv("REQUEST_TOKEN_BUDGET", "8000")

    token_manager = TokenManager(root=tmp_path)
    return token_manager, BudgetManager(token_manager)


# ----------------------------------------------------------------------
# main.py harness: fastapi / pydantic / httpx are not installed in the
# test venv, so main.py is imported against small stand-ins. LLM calls go
# to FakeAsyncClient.handler; nothing leaves the process.
# ----------------------------------------------------------------------

import types  # noqa: E402


class FakeHTTPError(Exception):
    pass


class FakeHTTPStatusError(FakeHTTPError):
    def __init__(self, message, *, request=None, response=None):
        super().__init__(message)
        self.request = request
        self.response = response


class FakeResponse:
    def __init__(self, status_code, body, headers=None):
        self.status_code = status_code
        self._body = body
        self.text = str(body)
        # httpx headers are case-insensitive; main.py looks keys up lowercase.
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise FakeHTTPStatusError(f"HTTP {self.status_code}", response=self)


class FakeAsyncClient:
    handler = None  # (url, json) -> (status, body[, headers]) | raises
    calls: list = []

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None, **kwargs):
        type(self).calls.append({"url": url, "json": json, "headers": headers,
                                 "timeout": kwargs.get("timeout")})
        return FakeResponse(*type(self).handler(url, json))


def fake_get(url, **kwargs):
    """Stand-in for httpx.get (health probe); FakeAsyncClient.get_handler: url -> (status, body)."""
    handler = getattr(FakeAsyncClient, "get_handler", None)
    if handler is None:
        raise FakeHTTPError("connection refused")
    return FakeResponse(*handler(url))


def ok_handler(url, payload):
    return 200, {
        "model": payload["model"],
        "choices": [{"message": {"role": "assistant", "content": "tamam"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


@pytest.fixture
def orch(tmp_path, monkeypatch):
    """Freshly imported main.py wired to tmp dirs and a fake LLM client."""
    vault = tmp_path / "vault"
    for worker in ("ares", "jarvis", "gordon", "oracle"):
        (vault / "workers" / worker).mkdir(parents=True)
        (vault / "workers" / worker / f"{worker}.md").write_text(f"# {worker}\n")

    monkeypatch.setenv("WORKERS_ROOT", str(vault))
    monkeypatch.setenv("TOKEN_USAGE_ROOT", str(vault / "usage"))
    monkeypatch.setenv("MODEL", "env/last-resort")
    for name, value in (("WORKER_DAILY_TOKEN_BUDGET", "50000"), ("ARES_DAILY_TOKEN_BUDGET", "50000"),
                        ("SYSTEM_DAILY_TOKEN_BUDGET", "200000"), ("TASK_TOKEN_BUDGET", "100000"),
                        ("REQUEST_TOKEN_BUDGET", "50000")):
        monkeypatch.setenv(name, value)

    class FastAPI:
        def __init__(self, **kwargs):
            pass

        def _route(self, *args, **kwargs):
            return lambda func: func

        get = post = delete = _route

    class HTTPException(Exception):
        def __init__(self, status_code, detail=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class BaseModel:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    class Response:
        def __init__(self, content=b"", media_type=None, headers=None, status_code=200):
            self.body, self.media_type, self.headers, self.status_code = content, media_type, headers or {}, status_code

    FakeAsyncClient.handler = staticmethod(ok_handler)
    FakeAsyncClient.get_handler = None
    FakeAsyncClient.calls = []

    stubs = {
        "fastapi": types.SimpleNamespace(FastAPI=FastAPI, HTTPException=HTTPException),
        "fastapi.responses": types.SimpleNamespace(Response=Response),
        "pydantic": types.SimpleNamespace(BaseModel=BaseModel, Field=lambda *a, **k: None),
        "httpx": types.SimpleNamespace(AsyncClient=FakeAsyncClient, HTTPError=FakeHTTPError,
                                       HTTPStatusError=FakeHTTPStatusError, get=fake_get),
    }
    for name, module in stubs.items():
        monkeypatch.setitem(sys.modules, name, module)
    # token_manager reads TOKEN_USAGE_ROOT at import time; reload it (and
    # main) for this test only. monkeypatch restores the originals.
    for name in ("main", "token_manager"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    import main

    main.fake_client = FakeAsyncClient
    main.usage_file = vault / "usage" / "usage.jsonl"
    return main
