"""Shared fixtures. Every test is offline: real sockets are blocked."""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import anthropic  # noqa: E402
import httpx2  # noqa: E402


class _NetworkBlocked(RuntimeError):
    pass


ANTHROPIC_ENV_VARS = (
    # credentials and endpoints the SDK (anthropic 2.x) reads from the environment
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_IDENTITY_TOKEN",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_WEBHOOK_SIGNING_KEY",
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch, tmp_path):
    """Fail loudly if anything tries to open a real network connection.

    Also hides every real credential: SDK credential env vars are removed and
    ANTHROPIC_CONFIG_DIR points at an empty temporary folder, so the SDK cannot
    load a profile from disk. Test clients always pass api_key="test-key".
    """

    def guard(*args, **kwargs):
        raise _NetworkBlocked("network access is disabled in tests")

    # Name resolution is blocked (the API host is a name, so any real request
    # fails before connecting). socket.connect itself is left alone because the
    # Windows asyncio event loop uses a local socketpair internally.
    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket, "getaddrinfo", guard)
    # Make sure no real credential or endpoint can be picked up from the environment:
    # the explicit list, every other ANTHROPIC_* variable (anthropic 2.4.0 also reads
    # ANTHROPIC_WORKSPACE_ID, ANTHROPIC_FOUNDRY_API_KEY, ...) and the cloud-platform
    # credentials the SDK's Bedrock / Vertex clients would read.
    import os

    for var in set(ANTHROPIC_ENV_VARS) | {v for v in os.environ if v.upper().startswith("ANTHROPIC_")} | {
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
    }:
        monkeypatch.delenv(var, raising=False)
    empty_cfg = tmp_path / "_empty_anthropic_config"
    empty_cfg.mkdir()
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(empty_cfg))
    yield


class MockAPI:
    """Scripted fake Anthropic API behind httpx2.MockTransport.

    ``responses`` is a list of callables or (status, json, headers) tuples
    consumed in order for POST /v1/messages; every request is recorded.
    """

    def __init__(self) -> None:
        self.message_responses: list[Any] = []
        self.requests: list[httpx2.Request] = []
        self.routes: dict[tuple[str, str], Callable[[httpx2.Request], httpx2.Response]] = {}

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        for (method, prefix), fn in self.routes.items():
            if request.method == method and path.startswith(prefix):
                return fn(request)
        if request.method == "POST" and path == "/v1/messages":
            if not self.message_responses:
                raise AssertionError("unexpected extra request to /v1/messages")
            item = self.message_responses.pop(0)
            if callable(item):
                return item(request)
            status, body, headers = item
            return httpx2.Response(status, json=body, headers=headers or {})
        raise AssertionError(f"unexpected request {request.method} {path}")

    def message_calls(self) -> list[httpx2.Request]:
        return [r for r in self.requests if r.url.path == "/v1/messages"]

    def client(self) -> anthropic.AsyncAnthropic:
        http = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self.handler))
        return anthropic.AsyncAnthropic(api_key="test-key", http_client=http, max_retries=0)


@pytest.fixture
def mock_api() -> MockAPI:
    return MockAPI()


def message_body(
    content: list[dict[str, Any]] | None = None,
    *,
    stop_reason: str = "end_turn",
    usage: dict[str, int] | None = None,
    stop_details: dict[str, Any] | None = None,
    model: str = "claude-haiku-5-5",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content if content is not None else [{"type": "text", "text": "ok"}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage
        or {"input_tokens": 1000, "output_tokens": 200, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }
    if stop_details is not None:
        body["stop_details"] = stop_details
    return body


def error_body(kind: str, message: str = "x", details: dict[str, Any] | None = None) -> dict[str, Any]:
    err: dict[str, Any] = {"type": kind, "message": message}
    if details:
        err["details"] = details
    return {"type": "error", "error": err, "request_id": "req_err"}


RATE_HEADERS = {
    "request-id": "req_ok",
    "anthropic-ratelimit-requests-limit": "1000",
    "anthropic-ratelimit-requests-remaining": "999",
    "anthropic-ratelimit-requests-reset": "2026-11-06T10:00:01Z",
    "anthropic-ratelimit-input-tokens-limit": "500000",
    "anthropic-ratelimit-input-tokens-remaining": "499000",
    "anthropic-ratelimit-input-tokens-reset": "2026-11-06T10:00:01Z",
    "anthropic-ratelimit-output-tokens-limit": "80000",
    "anthropic-ratelimit-output-tokens-remaining": "80000",
    "anthropic-ratelimit-output-tokens-reset": "2026-11-06T10:00:01Z",
}


class FakeClock:
    """Manual clock; ``sleep`` advances time instantly and records durations."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, dt: float) -> None:
        import asyncio

        self.sleeps.append(dt)
        self.t += max(0.0, dt)
        await asyncio.sleep(0)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


def read_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
