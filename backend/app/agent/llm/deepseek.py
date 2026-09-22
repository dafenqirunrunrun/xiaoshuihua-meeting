"""DeepSeekProvider — real internal LLM client (ADR D14).

Configuration is env-only (deepseek_base_url / deepseek_api_key /
deepseek_model, see app.config). We never read credentials from code, .env is
gitignored, and logs never print the key (core invariant #10).

Transport: sync `httpx.Client` calling an OpenAI-compatible
`POST {base_url}/chat/completions` with a JSON-body message and
`response_format={"type": "json_object"}`. This is the shape the internal
DeepSeek V4.1 Flash endpoint exposes (PRD §8). The response is read
defensively — any deviation (HTTP error, missing choices, non-string content)
becomes a `SummaryGenerationError`, not a 500.

Lazy: the provider does NOT touch the network at construction; it only fails
on the first `complete()` call, so import/startup/tests are never blocked by an
unset API key.
"""

from __future__ import annotations

import httpx
import time

from app.config import settings
from .base import SummaryGenerationError

DEFAULT_TIMEOUT = settings.llm_timeout_seconds


class DeepSeekProvider:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        retry_timeouts: bool = True,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        thinking_enabled: bool | None = None,
    ) -> None:
        # Allow explicit override (tests) but default to env-only config.
        self._base_url = base_url if base_url is not None else settings.deepseek_base_url
        self._api_key = api_key if api_key is not None else settings.deepseek_api_key
        self._model = model if model is not None else settings.deepseek_model
        self._timeout = timeout_seconds if timeout_seconds is not None else DEFAULT_TIMEOUT
        self._max_retries = (
            max(0, max_retries)
            if max_retries is not None
            else max(0, settings.llm_max_retries)
        )
        self._retry_timeouts = retry_timeouts
        self._temperature = (
            temperature if temperature is not None else settings.llm_temperature
        )
        self._max_output_tokens = (
            max_output_tokens
            if max_output_tokens is not None
            else settings.llm_max_output_tokens
        )
        self._thinking_enabled = thinking_enabled
        # Reuse DNS/TCP/TLS connections across realtime update windows.
        self._client = httpx.Client(timeout=self._timeout)

    @property
    def name(self) -> str:
        return self._model

    def complete(self, prompt: str, *, timeout_seconds: float | None = None) -> str:
        if not self._base_url or not self._api_key:
            raise SummaryGenerationError(
                "DeepSeek provider not configured (deepseek_base_url / deepseek_api_key). "
                "Set env vars or inject a FakeProvider in tests."
            )

        url = f"{self._base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": self._temperature,
            "max_tokens": self._max_output_tokens,
        }
        if self._thinking_enabled is not None:
            payload["thinking"] = {
                "type": "enabled" if self._thinking_enabled else "disabled"
            }
            # The internal gateway exposes vLLM-shaped response fields. These
            # equivalent hints cover both DeepSeek-native and vLLM chat
            # templates; unsupported extras are ignored by compatible servers.
            payload["reasoning_effort"] = (
                "high" if self._thinking_enabled else "none"
            )
            payload["chat_template_kwargs"] = {
                "enable_thinking": self._thinking_enabled
            }
        timeout = timeout_seconds if timeout_seconds is not None else self._timeout

        for attempt in range(self._max_retries + 1):
            try:
                resp = self._client.post(
                    url,
                    headers=self._auth_headers(),
                    json=payload,
                    timeout=timeout,
                )
                resp.raise_for_status()
                return extract_content(resp.json())
            except httpx.TimeoutException as exc:
                if self._retry_timeouts and attempt < self._max_retries:
                    self._retry_pause(attempt)
                    continue
                raise SummaryGenerationError(
                    f"deepseek request timed out after {timeout}s"
                ) from exc
            except httpx.HTTPStatusError as exc:
                retryable = (
                    exc.response.status_code == 429
                    or exc.response.status_code >= 500
                )
                if retryable and attempt < self._max_retries:
                    self._retry_pause(attempt)
                    continue
                raise SummaryGenerationError(
                    f"deepseek HTTP {exc.response.status_code}: "
                    f"{exc.response.text[:200]}"
                ) from exc
            except httpx.TransportError as exc:
                if attempt < self._max_retries:
                    self._retry_pause(attempt)
                    continue
                raise SummaryGenerationError(
                    f"deepseek transport/response error: {exc}"
                ) from exc
            except (ValueError, SummaryGenerationError) as exc:
                if isinstance(exc, SummaryGenerationError):
                    raise
                raise SummaryGenerationError(
                    f"deepseek response error: {exc}"
                ) from exc

        raise SummaryGenerationError("deepseek request exhausted retries")

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _retry_pause(attempt: int) -> None:
        # One short bounded backoff is enough for the MVP; retries remain finite
        # and only cover transient transport/429/5xx or malformed responses.
        time.sleep(min(0.25 * (2**attempt), 1.0))

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}


def extract_content(data: dict) -> str:
    """Defensively pull `choices[0].message.content` from a chat completion.

    Raises SummaryGenerationError on any shape deviation. Exported for the
    parser unit test without spinning up a server.
    """
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SummaryGenerationError(f"deepseek response missing content: {str(data)[:200]}") from exc
    if not isinstance(content, str) or not content.strip():
        choice = data.get("choices", [{}])[0]
        finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        completion_tokens = usage.get("completion_tokens")
        details = usage.get("completion_tokens_details")
        reasoning_tokens = details.get("reasoning_tokens") if isinstance(details, dict) else None
        diagnostics = ", ".join(
            f"{name}={value}"
            for name, value in (
                ("finish_reason", finish_reason),
                ("completion_tokens", completion_tokens),
                ("reasoning_tokens", reasoning_tokens),
            )
            if value is not None
        )
        suffix = f" ({diagnostics})" if diagnostics else ""
        raise SummaryGenerationError(f"deepseek returned empty content{suffix}")
    return content


__all__ = ["DeepSeekProvider", "extract_content"]
