from __future__ import annotations

import httpx
import pytest

from app.agent.llm.base import SummaryGenerationError
from app.agent.llm.deepseek import DeepSeekProvider
from app.agent.llm import deepseek as deepseek_module


class _FakeClient:
    def __init__(self, responses: list[httpx.Response], calls: list[dict]) -> None:
        self._responses = responses
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def post(self, url: str, *, headers: dict, json: dict) -> httpx.Response:
        self._calls.append({"url": url, "headers": headers, "json": json})
        return self._responses.pop(0)


def _response(status: int, payload: dict | None = None) -> httpx.Response:
    request = httpx.Request("POST", "https://llm.example/chat/completions")
    if payload is None:
        return httpx.Response(status, request=request, text="provider error")
    return httpx.Response(status, request=request, json=payload)


def test_real_provider_sends_stable_structured_generation_parameters(
    monkeypatch,
) -> None:
    calls: list[dict] = []
    responses = [
        _response(
            200,
            {"choices": [{"message": {"content": '{"overview":"ok"}'}}]},
        )
    ]
    monkeypatch.setattr(
        deepseek_module.httpx,
        "Client",
        lambda **_kwargs: _FakeClient(responses, calls),
    )
    provider = DeepSeekProvider(
        base_url="https://llm.example",
        api_key="secret",
        model="DeepSeek-Test",
        max_retries=0,
        temperature=0.1,
        max_output_tokens=2048,
    )

    assert provider.complete("prompt") == '{"overview":"ok"}'
    assert provider.name == "DeepSeek-Test"
    assert calls[0]["url"] == "https://llm.example/chat/completions"
    assert calls[0]["json"]["response_format"] == {"type": "json_object"}
    assert calls[0]["json"]["temperature"] == 0.1
    assert calls[0]["json"]["max_tokens"] == 2048


def test_transient_5xx_is_retried_once(monkeypatch) -> None:
    calls: list[dict] = []
    responses = [
        _response(503),
        _response(
            200,
            {"choices": [{"message": {"content": '{"overview":"ok"}'}}]},
        ),
    ]
    monkeypatch.setattr(
        deepseek_module.httpx,
        "Client",
        lambda **_kwargs: _FakeClient(responses, calls),
    )
    monkeypatch.setattr(DeepSeekProvider, "_retry_pause", staticmethod(lambda _n: None))
    provider = DeepSeekProvider(
        base_url="https://llm.example",
        api_key="secret",
        max_retries=1,
    )

    assert provider.complete("prompt") == '{"overview":"ok"}'
    assert len(calls) == 2


def test_non_retryable_4xx_fails_immediately(monkeypatch) -> None:
    calls: list[dict] = []
    responses = [_response(400)]
    monkeypatch.setattr(
        deepseek_module.httpx,
        "Client",
        lambda **_kwargs: _FakeClient(responses, calls),
    )
    provider = DeepSeekProvider(
        base_url="https://llm.example",
        api_key="secret",
        max_retries=2,
    )

    with pytest.raises(SummaryGenerationError, match="HTTP 400"):
        provider.complete("prompt")
    assert len(calls) == 1
