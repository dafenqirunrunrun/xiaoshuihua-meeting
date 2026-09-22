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

    def post(
        self, url: str, *, headers: dict, json: dict, timeout: float | None = None
    ) -> httpx.Response:
        self._calls.append({"url": url, "headers": headers, "json": json})
        return self._responses.pop(0)

    def close(self) -> None:
        return None


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


def test_provider_reuses_one_http_client_across_calls(monkeypatch) -> None:
    calls: list[dict] = []
    clients_created = 0
    responses = [
        _response(200, {"choices": [{"message": {"content": '{"ok":1}'}}]}),
        _response(200, {"choices": [{"message": {"content": '{"ok":2}'}}]}),
    ]

    def make_client(**_kwargs):
        nonlocal clients_created
        clients_created += 1
        return _FakeClient(responses, calls)

    monkeypatch.setattr(deepseek_module.httpx, "Client", make_client)
    provider = DeepSeekProvider(
        base_url="https://llm.example", api_key="secret", max_retries=0
    )

    assert provider.complete("one") == '{"ok":1}'
    assert provider.complete("two") == '{"ok":2}'
    assert clients_created == 1
    assert len(calls) == 2


def test_realtime_provider_can_disable_expensive_thinking(monkeypatch) -> None:
    calls: list[dict] = []
    responses = [
        _response(200, {"choices": [{"message": {"content": '{"ok":1}'}}]})
    ]
    monkeypatch.setattr(
        deepseek_module.httpx,
        "Client",
        lambda **_kwargs: _FakeClient(responses, calls),
    )
    provider = DeepSeekProvider(
        base_url="https://llm.example",
        api_key="secret",
        max_retries=0,
        thinking_enabled=False,
    )

    provider.complete("prompt")
    assert calls[0]["json"]["thinking"] == {"type": "disabled"}
    assert calls[0]["json"]["reasoning_effort"] == "none"
    assert calls[0]["json"]["chat_template_kwargs"] == {
        "enable_thinking": False
    }


def test_empty_content_reports_length_and_reasoning_budget(monkeypatch) -> None:
    calls: list[dict] = []
    responses = [
        _response(
            200,
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": None},
                    }
                ],
                "usage": {
                    "completion_tokens": 4096,
                    "completion_tokens_details": {"reasoning_tokens": 4096},
                },
            },
        )
    ]
    monkeypatch.setattr(
        deepseek_module.httpx,
        "Client",
        lambda **_kwargs: _FakeClient(responses, calls),
    )
    provider = DeepSeekProvider(
        base_url="https://llm.example", api_key="secret", max_retries=0
    )

    with pytest.raises(
        SummaryGenerationError,
        match=r"finish_reason=length, completion_tokens=4096, reasoning_tokens=4096",
    ):
        provider.complete("prompt")
