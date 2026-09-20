"""LLM-CONFIG-1 replay: the REAL chain, against the REAL configured provider.

    FINAL -> AGENT-1 -> DeepSeek -> Meeting State
          -> finish / force flush
          -> AGENT-2 -> DeepSeek -> Final Summary

This is NOT a regression test and is deliberately NOT under `tests/`: it makes
live, paid, NON-DETERMINISTIC calls to whatever `LLM_PROVIDER` points at. CI must
never run it (pytest pins `fake` precisely so it cannot). It exists to answer one
question that no offline suite can: does the AGENT-1 -> AGENT-2 chain actually
converge on the real model, with `summary_source=meeting_state`?

Run it explicitly, from `backend/`, with a real provider configured:

    .venv/Scripts/python scripts/replay_real_provider.py

It prints the meeting id, both agents' outcomes, the 10 acceptance checks, and
the plan log line. It NEVER prints the API key.

Read-only with respect to the app: it uses a throwaway temp SQLite DB and the
frozen six-segment script from the AGENT-2 spec.
"""

from __future__ import annotations

import logging
import os
import pathlib
import sys
import tempfile

_BACKEND = pathlib.Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
os.chdir(_BACKEND)

# A throwaway DB — never the developer's real one.
_TMP = tempfile.mkdtemp(prefix="llm_config1_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 'replay.db')}"
os.environ.setdefault("SSE_HEARTBEAT_INTERVAL", "60")
os.environ.setdefault("SSE_QUEUE_MAXSIZE", "100")
# NOTE: LLM_PROVIDER is deliberately NOT pinned here. Resolving the REAL
# provider is the entire point of this script.

# The frozen six-segment script (AGENT-2 spec §17 / LLM-CONFIG-1 §5).
SCRIPT = [
    "今天讨论上线时间",
    "我觉得下周一可能有点赶",
    "那就定下周三上线吧",
    "小王负责后端接口联调",
    "周二之前完成",
    "还有支付接口稳定性需要确认",
]

_RESULTS: list[tuple[str, bool, str]] = []

# §6 requires seeing `summary_source=meeting_state` in the LOGS, not merely
# inferring it from a COMPLETED summary — the fallback path also completes. So
# capture the plan log line and assert on it directly.
_PLAN_LINES: list[str] = []


class _PlanCapture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage().startswith("final_summary_plan"):
            _PLAN_LINES.append(record.getMessage())


def _install_plan_capture() -> None:
    handler = _PlanCapture()
    for name in ("app.agent.final_summary_service", "app.agent.finalizer"):
        lg = logging.getLogger(name)
        lg.addHandler(handler)
        lg.setLevel(logging.INFO)
        lg.propagate = False


def _record(case: str, ok: bool, detail: str = "") -> None:
    _RESULTS.append((case, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {case}" + (f" -> {detail}" if detail else ""))


def main() -> int:
    from fastapi.testclient import TestClient

    _install_plan_capture()

    from app.config import settings
    from app.db import Base, engine
    from app.main import app
    from app.meeting_state import service as meeting_state_service

    # Register every model on Base.metadata before create_all.
    from app.agent.summary_model import MeetingSummary  # noqa: F401
    from app.meeting.meeting_model import Meeting  # noqa: F401
    from app.meeting_state.model import MeetingState  # noqa: F401
    from app.transcript.segment_model import TranscriptSegment  # noqa: F401
    from app.transcript.stream_model import TranscriptStream  # noqa: F401

    print("=" * 72)
    print("LLM-CONFIG-1 real-provider replay")
    print("=" * 72)
    # §2 audit: what actually resolved. `deepseek_api_key` is NEVER printed.
    print(f"  llm_provider     = {settings.llm_provider!r}")
    print(f"  deepseek_model   = {settings.deepseek_model!r}")
    print(f"  final_summary_model = {settings.final_summary_model!r} (empty -> provider default)")
    print(f"  deepseek_base_url set = {bool(settings.deepseek_base_url)}")
    print(f"  deepseek_api_key  present = {bool(settings.deepseek_api_key)} (value never printed)")
    print(f"  llm_timeout_seconds = {settings.llm_timeout_seconds}")
    print(f"  meeting_agent_enabled = {settings.meeting_agent_enabled}")
    print(f"  final_summary_v2_enabled = {settings.final_summary_v2_enabled}")
    print()

    if settings.llm_provider != "deepseek":
        print(f"!! refusing to run: llm_provider={settings.llm_provider!r}, expected 'deepseek'.")
        print("   (This script is for the REAL provider; set LLM_PROVIDER=deepseek in backend/.env.)")
        return 2

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    with TestClient(app) as client:
        mid = client.post("/api/meetings", json={"title": "LLM-CONFIG-1 上线时间会议"}).json()["id"]
        client.post(f"/api/meetings/{mid}/start")
        print(f"meeting_id = {mid}\n")

        # --- Transcript: the six scripted FINAL segments ----------------------
        for i, text in enumerate(SCRIPT, start=1):
            r = client.post(
                f"/api/internal/meetings/{mid}/asr-events",
                json={
                    "meetingId": mid, "asrSessionId": "s1", "sentenceId": i,
                    "type": "transcript.final", "speakerId": 1,
                    "startMs": i * 1000, "endMs": i * 1000 + 500,
                    "text": text, "provider": "tencent",
                },
            )
            if r.status_code != 200:
                print(f"!! FINAL #{i} failed: {r.status_code} {r.text}")
                return 1
        print(f"ingested {len(SCRIPT)} FINAL segments")
        print()

        # --- AGENT-1 directly, so its own outcome is observable --------------
        # The TestClient runs BackgroundTasks synchronously, so the per-FINAL
        # state updates have already run by now. This extra call is a no-op
        # drain/idempotent check that also surfaces the agent's own report.
        from app.db import SessionLocal

        db = SessionLocal()
        try:
            report = meeting_state_service.process_pending(db, mid)
        finally:
            db.close()

        print(f"AGENT-1 drain (per-FINAL updates already ran): outcome={report.get('outcome')}")
        print()

        # --- finish + force flush --------------------------------------------
        # NOTE: this flush is an OBSERVATION, not the acceptance basis. The
        # Finalizer runs its OWN synchronous force flush (AGENT-2 §2 Step 0), so
        # a failure here (e.g. a provider timeout) can still be recovered by the
        # Finalizer and the summary can legitimately be `meeting_state`. Reading
        # the state HERE and asserting on it would therefore judge a stale
        # intermediate — which is exactly the false-negative this script used to
        # produce. The checks below read the state AFTER the Finalizer instead.
        client.post(f"/api/meetings/{mid}/finish")
        db = SessionLocal()
        try:
            flush = meeting_state_service.process_pending(db, mid, force_flush=True)
        finally:
            db.close()

        print(f"finish + explicit force flush: outcome={flush.get('outcome')} "
              f"reason={flush.get('reason')}")
        if flush.get("outcome") == meeting_state_service.OUTCOME_FAILED:
            # Surface it: a provider timeout here is worth knowing about even
            # though the Finalizer may recover it below.
            print("   ^ NOTE: this flush FAILED — the Finalizer's own flush is the "
                  "one under test; final state is read after it.")
        print()
        # --- STREAM_END -> Finalizer -> AGENT-2 --------------------------------
        r = client.post(
            f"/api/internal/meetings/{mid}/stream-end",
            json={
                "event_id": f"se_{mid}_6", "meeting_id": mid,
                "event_type": "TRANSCRIPT_STREAM_END", "last_segment_seq": 6,
            },
        )
        if r.status_code != 200:
            print(f"!! stream-end failed: {r.status_code} {r.text}")
            return 1

        summary = client.get(f"/api/meetings/{mid}/summary").json()
        meeting = client.get(f"/api/meetings/{mid}").json()

        # Read the FINAL state AFTER the Finalizer — its flush is the one that
        # counts (and is the one AGENT-2 §2 relies on). Same for the summary.
        db = SessionLocal()
        try:
            state = meeting_state_service.get_state(db, mid)
        finally:
            db.close()

    # --- the acceptance checks ----------------------------------------------
    print("Acceptance checks")
    print("-" * 72)

    import json

    # `get_state` returns an in-memory MeetingState with the content fields
    # flattened in (no `state_json` attribute), so dump the object whole.
    content = state.model_dump() if state else {}
    dump = json.dumps(content, ensure_ascii=False)
    cursor = state.last_processed_segment_seq if state else None

    status = state.status if state else None
    _record("1. Meeting State status is not FAILED", status != "FAILED", f"status={status}")
    _record(
        "2. last_processed_segment_seq reaches final transcript seq (6)",
        cursor == 6, f"cursor={cursor}",
    )

    _record("3. decision expresses 下周三上线", "下周三" in dump, _hit(dump, "下周三"))
    _record("4. action item expresses 后端接口联调", "联调" in dump, _hit(dump, "联调"))
    _record("5. owner is 小王", "小王" in dump, _hit(dump, "小王"))
    _record("6. deadline is 周二之前", "周二" in dump, _hit(dump, "周二"))
    _record("7. risk/open question expresses 支付接口稳定性",
            "支付" in dump, _hit(dump, "支付"))
    _record("8. source_segment_seqs present",
            '"source_segment_seqs"' in dump and '"source_segment_seqs": []' not in dump,
            "present" if '"source_segment_seqs"' in dump else "ABSENT")
    _record("9. Final Summary succeeded", summary.get("status") == "COMPLETED",
            f"status={summary.get('status')}")
    _record("10. meeting COMPLETED", meeting.get("status") == "COMPLETED",
            f"status={meeting.get('status')}")

    # 11/12 — §6: the LOG must show the meeting-state path, not the fallback.
    # Checked from the captured plan line, so it cannot be satisfied by luck.
    plan = _PLAN_LINES[-1] if _PLAN_LINES else ""
    _record("11. plan log shows summary_source=meeting_state",
            "summary_source=meeting_state" in plan,
            plan or "NO final_summary_plan LINE CAPTURED")
    _record("12. did NOT secretly take transcript_fallback",
            bool(plan) and "transcript_fallback" not in plan,
            "no fallback in plan" if plan else "NO PLAN LINE")

    print()
    print("--- Meeting State (raw) ---")
    print(json.dumps(content, ensure_ascii=False, indent=2))
    print()
    print("--- Summary (persisted legacy projection) ---")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print()

    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    print("=" * 72)
    print(f"TOTAL {passed}/{total} checks passed")
    print("=" * 72)
    return 0 if passed == total else 1


def _hit(dump: str, needle: str) -> str:
    """Short context window around the first hit, for the report line."""
    i = dump.find(needle)
    if i < 0:
        return f"{needle!r} NOT FOUND"
    return "..." + dump[max(0, i - 60): i + 60].replace("\n", " ") + "..."


if __name__ == "__main__":
    raise SystemExit(main())
