#!/usr/bin/env python
"""Step 6 integration tests — Transcript Freeze + Finalization Readiness.

Two layers, one run:

Part A (in-process service layer, temp sqlite DB):
  F01  PARTIAL after freeze -> TranscriptConflictError 409, cache untouched
  F02  NEW FINAL after freeze -> TranscriptConflictError 409
  F03  canonical existing FINAL retry after freeze -> idempotent 200 (no dup row)
  F04  same STREAM_END retry -> idempotent (200 no-op, no re-clear)
  F05  readiness: FINALIZING + ACCEPTED stream-end -> ready (derived, NOT persisted)
  F06  readiness: FINALIZING / no stream-end -> NOT ready
  F07  accepted STREAM_END -> live_utterance_cache cleared for the meeting
  F08  FAILURE path: STREAM_END with a gap -> 409 -> PARTIAL cache still present
  F09  readiness survives restart: new Session on same DB file -> still ready
  F10  meeting STAYS FINALIZING after freeze (never COMPLETED)

Part B (black-box HTTP against a subprocess uvicorn, fresh temp DB):
  H01  freeze 后 PARTIAL -> 409
  H02  freeze 后新 FINAL -> 409
  H03  freeze 后 canonical retry -> 200 (incl. after Finalizer completes meeting)
  H04  same STREAM_END retry -> 200
  H05  缺口 STREAM_END -> 409 (missing=[...])
  H06  meeting COMPLETED via Finalizer after freeze (Step 7B: summary terminal
       drives FINALIZING -> COMPLETED; freeze alone never completes it)

The Step 5 SSE regression (test_sse_step5.py: 21 cases + 2 parser) is run
separately — this file focuses on Step 6 only.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

# Make `app` importable and run from backend before any `app.*` import so the
# Part A DATABASE_URL env is honored by the app.db engine.
_BACKEND = pathlib.Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
os.chdir(_BACKEND)

_TMP = tempfile.mkdtemp(prefix="step6_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 'part_a.db')}"
os.environ.setdefault("SSE_HEARTBEAT_INTERVAL", "1.0")
os.environ.setdefault("SSE_QUEUE_MAXSIZE", "3")

# --- result collector ---------------------------------------------------------

_RESULTS: list[tuple[str, bool, str]] = []


def _record(case: str, ok: bool, detail: str = ""):
    _RESULTS.append((case, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {case}" + (f" — {detail}" if detail else ""))
    sys.stdout.flush()


def _fail(case: str, detail: str = ""):
    _record(case, False, detail)


# ==== Part A: in-process service-layer tests =================================


def _part_a() -> int:
    from app.meeting.meeting_model import Meeting
    from app.db import SessionLocal, create_all
    from app.transcript import repository as transcript_repository
    from app.transcript import schemas
    from app.meeting import service as meeting_service
    from app.transcript import service as transcript_service
    from app.transcript import stream_service
    from app.transcript.live_cache import live_utterance_cache

    create_all()
    db = SessionLocal()

    def mk(n: int) -> str:
        return f"meeting_step6_{n}"

    def seed(n: int, title: str) -> None:
        db.add(Meeting(id=mk(n), title=title, status="CREATED"))
        db.commit()

    def final(mid: str, seq: int, text: str, event_id: str, *, expect_conflict: bool = False) -> bool:
        try:
            transcript_service.handle_event(
                db, mid,
                schemas.TranscriptEvent(
                    event_id=event_id, meeting_id=mid, utterance_id=f"utt_{seq}",
                    segment_seq=seq, speaker_id="sp1", text=text, status="FINAL",
                ),
            )
            return not expect_conflict
        except Exception as e:
            return expect_conflict and type(e).__name__ == "TranscriptConflictError"

    def partial(mid: str, utt: str, text: str, *, expect_conflict: bool = False) -> bool:
        try:
            transcript_service.handle_event(
                db, mid,
                schemas.TranscriptEvent(
                    event_id=f"evt_p_{utt}", meeting_id=mid, utterance_id=utt,
                    speaker_id="sp1", text=text, status="PARTIAL",
                ),
            )
            return not expect_conflict
        except Exception as e:
            return expect_conflict and type(e).__name__ == "TranscriptConflictError"

    # --- F01/F03/F04/F05/F07/F10 on a single meeting -------------------------
    seed(1, "F1")
    mid = mk(1)
    meeting_service.start_meeting(db, mid)
    meeting_service.finish_meeting(db, mid)
    final(mid, 1, "第一句", "evt_f1")
    partial(mid, "utt_x", "残留partial")

    # accepted STREAM_END -> cache cleared (F07)
    se = stream_service.handle_stream_end(
        db, mid, schemas.StreamEndEvent(event_id="evt_se1", meeting_id=mid, last_segment_seq=1)
    )
    _record("F07 accepted STREAM_END clears meeting PARTIAL cache",
            live_utterance_cache.get(mid, "utt_x") is None,
            f"utt_x_in_cache={live_utterance_cache.get(mid, 'utt_x') is not None}")

    # PARTIAL after freeze -> 409, cache untouched (F01)
    _record("F01 PARTIAL after freeze -> 409",
            partial(mid, "utt_y", "freeze后partial", expect_conflict=True))
    _record("F01 rejected PARTIAL did not touch cache",
            live_utterance_cache.get(mid, "utt_y") is None)

    # NEW FINAL after freeze -> 409 (F02)
    _record("F02 new FINAL after freeze -> 409",
            final(mid, 2, "新的一句", "evt_new2", expect_conflict=True))

    # canonical FINAL retry after freeze -> idempotent (F03)
    _record("F03 canonical retry (same event_id) after freeze -> 200/ok",
            final(mid, 1, "第一句", "evt_f1"))
    _record("F03b canonical retry (alt event_id) after freeze -> 200/ok",
            final(mid, 1, "第一句", "evt_f1_alt"))
    _record("F03 no duplicate row after retries",
            sum(1 for s in transcript_repository.list_sorted(db, mid) if s.segment_seq == 1) == 1)

    # same STREAM_END retry -> 200 no-op (F04)
    se2 = stream_service.handle_stream_end(
        db, mid, schemas.StreamEndEvent(event_id="evt_se1_again", meeting_id=mid, last_segment_seq=1)
    )
    _record("F04 same STREAM_END retry -> 200/ok", se2 is not None and se2[2] is True)

    # readiness derived (F05) — no new persisted flag needed
    rd = stream_service.get_finalization_readiness(db, mid)
    _record("F05 readiness true (FINALIZING + ACCEPTED + last seq persisted)",
            rd["finalization_ready"] is True and rd["stream_ended"] is True
            and rd["meeting_status"] == "FINALIZING" and rd["last_segment_seq"] == 1,
            f"rd={rd}")

    # meeting stays FINALIZING (F10)
    m = db.get(Meeting, mid)
    _record("F10 meeting stays FINALIZING after freeze",
            m is not None and m.status == "FINALIZING", f"status={m.status if m else None}")

    # --- F09: readiness survives restart (new Session, same DB file) ---------
    db.close()
    db2 = SessionLocal()
    rd2 = stream_service.get_finalization_readiness(db2, mid)
    m2 = db2.get(Meeting, mid)
    _record("F09 readiness survives restart (new Session, same DB file)",
            rd2["finalization_ready"] is True and m2 is not None and m2.status == "FINALIZING",
            f"rd2={rd2} status={m2.status if m2 else None}")
    db2.close()
    db = SessionLocal()

    # --- F06: FINALIZING but NO stream-end -> NOT ready ----------------------
    seed(2, "F6")
    mid2 = mk(2)
    meeting_service.start_meeting(db, mid2)
    meeting_service.finish_meeting(db, mid2)
    rd3 = stream_service.get_finalization_readiness(db, mid2)
    _record("F06 readiness false (FINALIZING, no stream-end)",
            rd3["finalization_ready"] is False and rd3["stream_ended"] is False,
            f"rd3={rd3}")

    # --- F08: FAILURE path — stream-end with a gap -> 409, cache SURVIVES ----
    seed(3, "F8")
    mid3 = mk(3)
    meeting_service.start_meeting(db, mid3)
    meeting_service.finish_meeting(db, mid3)
    final(mid3, 1, "只有1", "evt_f8a")
    partial(mid3, "utt_g", "没被冻住的partial")
    try:
        stream_service.handle_stream_end(
            db, mid3, schemas.StreamEndEvent(event_id="evt_se_gap", meeting_id=mid3, last_segment_seq=2)
        )
        _fail("F08 stream-end gap -> 409", "no exception")
    except Exception as e:
        _record("F08 stream-end gap -> 409",
                type(e).__name__ == "TranscriptConflictError",
                f"exc={type(e).__name__}")
    _record("F08 PARTIAL cache SURVIVES failed freeze (not cleared)",
            live_utterance_cache.get(mid3, "utt_g") is not None,
            f"utt_g_in_cache={live_utterance_cache.get(mid3, 'utt_g') is not None}")

    db.close()
    return 0


# ==== Part B: black-box HTTP against a subprocess server =====================

HOST = "127.0.0.1"
PORT = 8766
BASE = f"http://{HOST}:{PORT}"


def _http_request(method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
    url = BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def _new_meeting(title: str) -> dict:
    code, body = _http_request("POST", "/api/meetings", {"title": title})
    assert code == 201, f"create failed {code}: {body}"
    return body


def _start(mid: str):
    code, body = _http_request("POST", f"/api/meetings/{mid}/start")
    assert code in (200, 201), f"start failed {code}: {body}"


def _finish(mid: str):
    code, body = _http_request("POST", f"/api/meetings/{mid}/finish")
    assert code in (200, 201), f"finish failed {code}: {body}"


def _final(mid: str, seq: int, text: str, event_id: str) -> int:
    code, _ = _http_request(
        "POST", f"/api/internal/meetings/{mid}/transcript-events",
        {"event_id": event_id, "meeting_id": mid, "utterance_id": f"utt_{seq}",
         "segment_seq": seq, "speaker_id": "sp1", "text": text, "status": "FINAL"},
    )
    return code


def _partial(mid: str, utt: str, text: str) -> int:
    code, _ = _http_request(
        "POST", f"/api/internal/meetings/{mid}/transcript-events",
        {"event_id": f"evt_p_{utt}", "meeting_id": mid, "utterance_id": utt,
         "speaker_id": "sp1", "text": text, "status": "PARTIAL"},
    )
    return code


def _stream_end(mid: str, last: int, event_id: str) -> tuple[int, dict | str]:
    return _http_request(
        "POST", f"/api/internal/meetings/{mid}/stream-end",
        {"event_id": event_id, "meeting_id": mid, "event_type": "TRANSCRIPT_STREAM_END",
         "last_segment_seq": last},
    )


def _part_b() -> int:
    m = _new_meeting("H01-H04")
    _start(m["id"])
    _finish(m["id"])
    assert _final(m["id"], 1, "第一句", "evt_h1") == 200
    se_code, se_body = _stream_end(m["id"], 1, "evt_se_h")
    _record("H04 first stream-end accepted -> 200", se_code == 200, f"code={se_code} body={se_body}")

    se2, _ = _stream_end(m["id"], 1, "evt_se_h_again")
    _record("H04b same STREAM_END retry -> 200", se2 == 200, f"code={se2}")

    _record("H01 PARTIAL after freeze -> 409", _partial(m["id"], "utt_h", "freeze后partial") == 409)
    _record("H02 new FINAL after freeze -> 409", _final(m["id"], 99, "新的一句", "evt_h_new99") == 409)
    _record("H03 canonical retry after freeze -> 200", _final(m["id"], 1, "第一句", "evt_h1") == 200)

    m2 = _new_meeting("H05")
    _start(m2["id"])
    _finish(m2["id"])
    assert _final(m2["id"], 1, "只有1", "evt_h05_1") == 200
    g_code, g_body = _stream_end(m2["id"], 2, "evt_se_h05")  # missing=[2]
    _record("H05 gap stream-end -> 409 (missing segments)",
            g_code == 409 and "missing" in str(g_body).lower(),
            f"code={g_code} body={g_body}")

    # H06 (Step 7B contract): freeze does NOT complete the meeting by itself;
    # the Finalizer drives FINALIZING -> COMPLETED AFTER the summary is terminal
    # (Step 7B §9). On the real HTTP path the first accepted STREAM_END schedules
    # the Finalizer as a BackgroundTask; poll until it lands on a terminal state.
    final_status = None
    for _ in range(50):
        code, body = _http_request("GET", f"/api/meetings/{m['id']}")
        if code == 200:
            final_status = body.get("status")
            if final_status == "COMPLETED":
                break
        time.sleep(0.2)
    _record("H06 meeting COMPLETED via Finalizer after freeze (summary terminal)",
            final_status == "COMPLETED",
            f"status={final_status}")
    return 0


# ==== server harness (fresh temp DB for Part B) ==============================

def _run_server() -> subprocess.Popen:
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 'part_b.db')}"
    env["SSE_HEARTBEAT_INTERVAL"] = "1.0"
    env["SSE_QUEUE_MAXSIZE"] = "3"
    logf = open(os.path.join(_TMP, "part_b_server.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", HOST, "--port", str(PORT)],
        cwd=_BACKEND, env=env, stdout=logf, stderr=subprocess.STDOUT,
    )
    proc._test_logf = logf
    return proc


def _wait_server(timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            code, _ = _http_request("GET", "/api/health")
            if code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


if __name__ == "__main__":
    rc_a = _part_a()
    rc_b = 0
    proc_b = _run_server()
    try:
        if not _wait_server():
            _fail("Part B server did not start")
            rc_b = 2
        else:
            rc_b = _part_b()
    finally:
        proc_b.terminate()
        try:
            proc_b.wait(timeout=5)
        except Exception:
            proc_b.kill()

    print("\n==== Step 6 (freeze + readiness) results ====")
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    failed = len(_RESULTS) - passed
    for case, ok, detail in _RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {case}" + (f" — {detail}" if detail else ""))
    print(f"\nTOTAL {len(_RESULTS)} cases: {passed} PASS / {failed} FAIL")
    sys.exit(0 if failed == 0 and rc_a == 0 and rc_b == 0 else 1)



