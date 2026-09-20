"""Stability probe for the real provider (LLM-CONFIG-1 §7).

One green replay proves the chain CAN converge. It does not tell you whether the
model's structured output is STABLE, nor whether a repair retry was silently
carrying the run. This script runs the same six-segment chain N times and, for
each run, counts the provider calls each agent made.

Why counting matters: `SUMMARY_MAX_ATTEMPTS` (default 2) means a run can succeed
on the SECOND attempt after a repair prompt. A `COMPLETED` summary alone cannot
distinguish "parsed first try" from "repaired once" — both are green. Counting
calls separates them.

The counter wraps the REAL provider; it never replaces it, so this still
exercises DeepSeek. It patches only the replay process, never production code.

    .venv/Scripts/python scripts/replay_real_provider_stability.py [runs]
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

_BACKEND = pathlib.Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
os.chdir(_BACKEND)

_TMP = tempfile.mkdtemp(prefix="llm_config1_stab_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 'stab.db')}"
os.environ.setdefault("SSE_HEARTBEAT_INTERVAL", "60")
os.environ.setdefault("SSE_QUEUE_MAXSIZE", "100")

SCRIPT = [
    "今天讨论上线时间",
    "我觉得下周一可能有点赶",
    "那就定下周三上线吧",
    "小王负责后端接口联调",
    "周二之前完成",
    "还有支付接口稳定性需要确认",
]


class CountingProvider:
    """Transparent proxy: delegates to the real provider, counts calls."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.name = getattr(inner, "name", "unknown")
        self.calls = 0

    def complete(self, prompt: str, *, timeout_seconds: float | None = None) -> str:
        self.calls += 1
        return self._inner.complete(prompt, timeout_seconds=timeout_seconds)


def main() -> int:
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3

    from fastapi.testclient import TestClient

    from app.config import settings
    from app.db import Base, engine, SessionLocal
    from app.main import app
    from app.meeting_state import service as meeting_state_service

    from app.agent.summary_model import MeetingSummary  # noqa: F401
    from app.meeting.meeting_model import Meeting  # noqa: F401
    from app.meeting_state.model import MeetingState  # noqa: F401
    from app.transcript.segment_model import TranscriptSegment  # noqa: F401
    from app.transcript.stream_model import TranscriptStream  # noqa: F401

    if settings.llm_provider != "deepseek":
        print(f"!! refusing: llm_provider={settings.llm_provider!r}, expected 'deepseek'")
        return 2

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    real = _real_provider()
    print(f"provider = {real.name!r}  model = {real._model!r}  runs = {runs}")
    print()

    summary_counts: list[int] = []
    state_counts: list[int] = []
    verdicts: list[str] = []

    with TestClient(app) as client:
        for run in range(1, runs + 1):
            # One counter per agent, re-wrapped per run so the numbers are
            # attributable. AGENT-1 resolves its provider INSIDE `process_pending`
            # (`from app.agent.llm.factory import get_provider` at call time), so
            # patching the FACTORY reaches it. AGENT-2 binds `get_provider` at
            # module import in summary_service, so patching the factory does NOT
            # reach it — patch that module's binding too.
            state_counter = CountingProvider(real)
            summary_counter = CountingProvider(real)

            import app.agent.llm.factory as llm_factory
            llm_factory.get_provider = lambda model=None: state_counter
            import app.agent.summary_service as summary_service
            summary_service.get_provider = lambda model=None: summary_counter

            mid = client.post(
                "/api/meetings", json={"title": f"stability run {run}"}
            ).json()["id"]
            client.post(f"/api/meetings/{mid}/start")

            for i, text in enumerate(SCRIPT, start=1):
                client.post(
                    f"/api/internal/meetings/{mid}/asr-events",
                    json={
                        "meetingId": mid, "asrSessionId": "s1", "sentenceId": i,
                        "type": "transcript.final", "speakerId": 1,
                        "startMs": i * 1000, "endMs": i * 1000 + 500,
                        "text": text, "provider": "tencent",
                    },
                )

            client.post(f"/api/meetings/{mid}/finish")
            db = SessionLocal()
            try:
                meeting_state_service.process_pending(db, mid, force_flush=True)
                st = meeting_state_service.get_state(db, mid)
            finally:
                db.close()

            client.post(
                f"/api/internal/meetings/{mid}/stream-end",
                json={
                    "event_id": f"se_{mid}_6", "meeting_id": mid,
                    "event_type": "TRANSCRIPT_STREAM_END", "last_segment_seq": 6,
                },
            )
            summary = client.get(f"/api/meetings/{mid}/summary").json()

            # Provider calls observed by the SUMMARY agent only. AGENT-1's
            # per-FINAL background updates are counted separately.
            scalls = summary_counter.calls
            summary_counts.append(scalls)
            state_counts.append(state_counter.calls)

            # One call = parsed first try. Two = a repair retry was needed.
            if summary.get("status") == "COMPLETED" and scalls == 1:
                verdict = "COMPLETED (1st try)"
            elif summary.get("status") == "COMPLETED":
                verdict = f"COMPLETED after {scalls} calls (REPAIR)"
            else:
                verdict = f"{summary.get('status')} — {summary.get('error_message', '')[:60]}"
            verdicts.append(verdict)

            cursor = st.last_processed_segment_seq if st else None
            print(f"  run {run}: {verdict}")
            print(f"          summary_provider_calls={scalls} "
                  f"state_provider_calls={state_counter.calls} "
                  f"cursor={cursor} state_status={st.status if st else None}")

    print()
    repairs = sum(1 for c in summary_counts if c > 1)
    all_done = all(v.startswith("COMPLETED") for v in verdicts)
    print(f"completed: {sum(1 for v in verdicts if v.startswith('COMPLETED'))}/{runs}")
    print(f"runs needing repair: {repairs}/{runs}")
    print(f"summary provider calls: min={min(summary_counts)} max={max(summary_counts)}")
    print(f"state provider calls:   min={min(state_counts)} max={max(state_counts)}")
    print()
    print("STABILITY:", "STABLE (all completed)" if all_done else "UNSTABLE — see verdicts")
    return 0 if all_done else 1


def _real_provider():
    """Build the real configured provider directly (no fake fallback)."""
    from app.agent.llm.deepseek import DeepSeekProvider
    from app.agent.llm.factory import get_provider

    provider = get_provider()
    if not isinstance(provider, DeepSeekProvider):
        raise SystemExit(f"expected DeepSeekProvider, got {type(provider).__name__}")
    return provider


if __name__ == "__main__":
    raise SystemExit(main())
