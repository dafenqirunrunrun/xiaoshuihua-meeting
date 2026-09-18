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

from app.config import settings
from .base import SummaryGenerationError

DEFAULT_TIMEOUT = settings.llm_timeout_seconds


class DeepSeekProvider:
    name = "deepseek-v4.1-flash"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        # Allow explicit override (tests) but default to env-only config.
        self._base_url = base_url if base_url is not None else settings.deepseek_base_url
        self._api_key = api_key if api_key is not None else settings.deepseek_api_key
        self._model = model if model is not None else settings.deepseek_model
        self._timeout = timeout_seconds if timeout_seconds is not None else DEFAULT_TIMEOUT

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
        }
        timeout = timeout_seconds if timeout_seconds is not None else self._timeout

        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, headers=self._auth_headers(), json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException as exc:
            raise SummaryGenerationError(f"deepseek request timed out after {timeout}s") from exc
        except httpx.HTTPStatusError as exc:
            raise SummaryGenerationError(
                f"deepseek HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except (httpx.TransportError, ValueError) as exc:
            raise SummaryGenerationError(f"deepseek transport error: {exc}") from exc

        return extract_content(data)

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
        raise SummaryGenerationError("deepseek returned empty content")
    return content


__all__ = ["DeepSeekProvider", "extract_content"]
