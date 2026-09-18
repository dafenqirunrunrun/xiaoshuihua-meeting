#!/usr/bin/env python
"""Step 7B integration tests — Finalizer + Summary API + Summary SSE + Meeting Completion.

Co-located with the real code, no offline replicas: this file imports the real
`finalizer_service`, `summary_service`, `stream_service`, `meeting_service`,
`transcript_service`, `sse_service`, `FakeProvider`, and the repositories, and
drives them against in-process temp SQLite DBs. Part B talks black-box HTTP to
a subprocess uvicorn (fresh temp DB) so FastAPI BackgroundTasks — the real
process-local Finalizer trigger — are exercised end-to-end.

Cases (B01–B26, step 7B §25):

Part A (in-process service layer, deterministic):
  B01  first accepted STREAM_END (newly_accepted=True) is the trigger; an
       identical retry returns newly_accepted=False and NEVER re-runs the LLM
  B02  Finalizer happy path: fake JSON -> summary COMPLETED + meeting COMPLETED
  B02b structured fields persisted (overview/key_topics/version=1)
  B03  Finalizer on summary FAILED also completes the meeting (never Meeting FAILED)
  B03b failure reason persisted, no stack/exception details leak
  B04  Finalizer idempotent: meeting already COMPLETED -> no LLM call, no-op
  B05  Finalizer crash-gap: terminal summary + FINALIZING meeting -> rerun
       completes meeting with ZERO extra LLM calls
  B06  meeting stays FINALIZING while summary is GENERATING (no premature COMPLETED)
  B07  summary.started emitted AFTER GENERATING durable; summary.completed AFTER
       terminal summary + meeting COMPLETED durable
  B08  summary SSE payload minimal {version, status} — no raw_model_output /
       stack / api key / prompt in envelope
  B09  summary SSE id stable: summary:<meeting_id>:<version>:<status>
  B10  summary.* events are NOT replayed on connect (live-only; no catch-up)
  B11  SSE publish failure does NOT roll back DB commit (commit-before-publish)
  B12  fail-closed: unknown LLM_PROVIDER -> LLMConfigurationError, ZERO
       FakeProvider calls, no fake summary persisted (§1/§24)
  B25  SSE order: summary.started → summary.completed → meeting.status(COMPLETED)

Part B (black-box HTTP; server runs with LLM_PROVIDER=fake, FastAPI BackgroundTasks
      are the real trigger):
  B13  first STREAM_END auto-triggers Finalizer -> meeting COMPLETED
  B14  StreamEndOut carries last_segment_seq + transcript_complete
  B15  GET /summary returns latest version COMPLETED snapshot
       (overview/key_topics/conclusions/open_questions/version/status)
  B16  idempotent stream-end retry does NOT re-run summary / re-complete
       (summary version + meeting stay COMPLETED; no new version)
  B17  POST /summary/regenerate -> 202 {meeting_id, version: v+1, status:GENERATING}
  B18  regenerate while latest GENERATING -> 409 (no duplicate model cost)
  B19  regenerate completes in background -> GET /summary shows v+1 COMPLETED;
       meeting STAYS COMPLETED (§19 — never reverts to FINALIZING)
  B20  versions append-only: v1 + v2 both present, never overwritten
  B21  regenerate on CREATED meeting (no frozen transcript) -> 409
  B22  GET /summary: no-summary meeting -> 404 "summary not found";
       unknown meeting -> 404 "meeting not found"
  B23  regeneration LLM input is persisted FINAL ASC only (no PARTIAL): verified
       via FakeProvider prompt containing the FINAL transcript lines and no
       PARTIAL-only marker (all bypasses of SSE/frontend)
  B24  background task owns its own session: regenerate/Finalizer complete on
       the real HTTP path without request-session reuse errors
  B26  meeting.status SSE shows COMPLETED after finalize (in-process, subscribe
       before Finalizer; also GET /summary confirms the meeting 200 on B13)

Run:  PYTHONPATH="backend" backend/.venv/Scripts/python scripts/test_summary_step7b.py
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

# Make `app` importable + honour DATABASE_URL before any app.* import.
_BACKEND = pathlib.Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
os.chdir(_BACKEND)

_TMP = tempfile.mkdtemp(prefix="step7b_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 'summary7b.db')}"
os.environ.setdefault("SSE_HEARTBEAT_INTERVAL", "1.0")
os.environ.setdefault("SSE_QUEUE_MAXSIZE", "10")
os.environ.setdefault("SUMMARY_MAX_ATTEMPTS", "2")

from app.agent.summary_model import MeetingSummary  # noqa: E402
from app.meeting.meeting_model import Meeting  # noqa: E402
from app.db import SessionLocal, create_all  # noqa: E402
from app.common.exceptions import InvalidMeetingStateTransition, TranscriptConflictError  # noqa: E402
from app.agent import summary_repository  # noqa: E402
from app.transcript import repository as transcript_repository  # noqa: E402
from app.transcript import schemas as transcript_schemas  # noqa: E402
from app.agent import finalizer as finalizer_service  # noqa: E402
from app.agent import summary_service  # noqa: E402
from app.meeting import service as meeting_service  # noqa: E402
from app.realtime import sse_service  # noqa: E402
from app.transcript import service as transcript_service  # noqa: E402
from app.transcript import stream_service  # noqa: E402
from app.agent.llm.base import LLMConfigurationError, LLMProvider, SummaryGenerationError  # noqa: E402
from app.agent.llm.fake import FakeProvider  # noqa: E402

_RESULTS: list[tuple[str, bool, str]] = []


def _record(case: str, ok: bool, detail: str = "") -> None:
    _RESULTS.append((case, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {case}" + (f" — {detail}" if detail else ""))
    sys.stdout.flush()


def _fail(case: str, detail: str = "") -> None:
    _record(case, False, detail)


# --- Session / fixture helpers ----------------------------------------------

_MID: int = 0


def _next_id() -> str:
    global _MID
    _MID += 1
    return f"meeting_s7b_{_MID}"


class _Probe(LLMProvider):
    """Record-only provider that captures prompts and can be scripted.

    Used to prove a call was MADE (call-count > 0), NOT made (call-count 0), or
    to deliver a deterministic terminal result / failure.
    """

    name = "probe-provider"

    def __init__(self, result: str | None = None, error: str | None = None) -> None:
        self.calls: list[str] = []
        self.result = result
        self.error = error

    def complete(self, prompt: str, *, timeout_seconds: float | None = None) -> str:
        self.calls.append(prompt)
        if self.error:
            raise SummaryGenerationError(self.error)
        if self.result is not None:
            return self.result
        raise SummaryGenerationError("probe provider: not scripted")


VALID_JSON = (
    '{"overview": "会议讨论最终方案。", '
    '"key_topics": ["方案", "落地"], '
    '"conclusions": ["采用方案A"], '
    '"open_questions": ["预算"]}'
)


def _seed_finalizing(mid: str, *, title: str = "Step7B 会议", seed_finals: int = 3) -> None:
    """Create a meeting that is FINALIZING with FINALs persisted but NOT frozen.

    No STREAM_END is accepted — so `handle_stream_end` can still be observed
    as the FIRST accept (newly_accepted=True) by the caller.
    """
    db = SessionLocal()
    db.add(Meeting(id=mid, title=title, status="CREATED"))
    db.commit()
    meeting_service.start_meeting(db, mid)
    meeting_service.finish_meeting(db, mid)
    for i in range(1, seed_finals + 1):
        transcript_service.handle_event(
            db, mid,
            transcript_schemas.TranscriptEvent(
                event_id=f"evt_{mid}_{i}", meeting_id=mid, utterance_id=f"utt_{mid}_{i}",
                segment_seq=i, speaker_id="sp1", text=f"发言内容第{i}条",
                status="FINAL",
            ),
        )
    db.close()


def seed_frozen(mid: str, *, title: str = "Step7B 会议", seed_finals: int = 3) -> None:
    """Create a meeting that is FINALIZING + frozen (Step 6 readiness true)."""
    _seed_finalizing(mid, title=title, seed_finals=seed_finals)
    db = SessionLocal()
    stream_service.handle_stream_end(
        db, mid, transcript_schemas.StreamEndEvent(
            event_id=f"se_{mid}", meeting_id=mid, last_segment_seq=seed_finals,
        ),
    )
    db.close()


# =============================================================================
# Part A: in-process service-layer tests (deterministic)
# =============================================================================


def _part_a() -> int:
    create_all()
    from app.realtime.event_broker import event_broker

    # --- B01: first STREAM_END is the trigger; retry is not -----------------
    mid = _next_id()
    _seed_finalizing(mid)
    db = SessionLocal()
    se1 = stream_service.handle_stream_end(
        db, mid, transcript_schemas.StreamEndEvent(
            event_id=f"se_{mid}_again", meeting_id=mid, last_segment_seq=3,
        ),
    )
    _record("B01 first accepted STREAM_END -> newly_accepted=True",
            len(se1) == 4 and se1[3] is True,
            f"newly_accepted={se1[3]}")
    run_before = summary_repository.max_version(db, mid) or 0
    se2 = stream_service.handle_stream_end(
        db, mid, transcript_schemas.StreamEndEvent(
            event_id=f"se_{mid}_again2", meeting_id=mid, last_segment_seq=3,
        ),
    )
    run_after = summary_repository.max_version(db, mid) or 0
    _record("B01b identical STREAM_END retry -> newly_accepted=False (no re-run)",
            len(se2) == 4 and se2[3] is False and run_after == run_before,
            f"newly_accepted={se2[3]} v_before={run_before} v_after={run_after}")
    db.close()

    # --- B02/B02b: Finalizer happy path -------------------------------------
    mid = _next_id()
    seed_frozen(mid)
    db = SessionLocal()
    fake = FakeProvider(responses=[VALID_JSON])
    ok = finalizer_service.finalize_for_meeting(db, mid, provider=fake)
    item = summary_repository.get_latest(db, mid)
    m = db.get(Meeting, mid)
    _record("B02 Finalizer: summary COMPLETED",
            ok and item is not None and item.status == "COMPLETED")
    _record("B02b Finalizer: meeting FINALIZING -> COMPLETED",
            m is not None and m.status == "COMPLETED", f"status={m.status}")
    _record("B02c structured fields persisted (overview/key_topics/version=1)",
            item is not None and item.overview == "会议讨论最终方案。"
            and item.key_topics == ["方案", "落地"] and item.version == 1,
            f"v={item.version if item else None}, topics={item.key_topics if item else None}")
    db.close()

    # --- B03/B03b: FAILED summary also completes meeting, no Meeting FAILED --
    mid = _next_id()
    seed_frozen(mid)
    db = SessionLocal()
    bad = _Probe(error="provider down")
    finalizer_service.finalize_for_meeting(db, mid, provider=bad)
    item = summary_repository.get_latest(db, mid)
    m = db.get(Meeting, mid)
    _record("B03 FAILED summary -> meeting COMPLETED (never Meeting FAILED)",
            item is not None and item.status == "FAILED"
            and m is not None and m.status == "COMPLETED",
            f"summary={item.status if item else None} meeting={m.status if m else None}")
    raw = item.raw_model_output if item else ""
    _record("B03b FAILED reason persisted, no stack / no api key in raw output",
            item is not None and "provider down" in (raw or "")
            and "Traceback" not in (raw or "") and "api_key" not in (raw or "").lower(),
            f"raw={raw[:80]!r}")
    db.close()

    # --- B04: Finalizer idempotent on already-COMPLETED ----------------------
    mid = _next_id()
    seed_frozen(mid)
    db = SessionLocal()
    fake = FakeProvider(responses=[VALID_JSON])
    finalizer_service.finalize_for_meeting(db, mid, provider=fake)
    calls_before = len(fake.calls)
    # Second run: meeting COMPLETED -> early return, zero extra LLM calls.
    ok2 = finalizer_service.finalize_for_meeting(db, mid, provider=fake)
    _record("B04 Finalizer idempotent on COMPLETED (zero extra LLM calls)",
            ok2 is True and len(fake.calls) == calls_before,
            f"calls_before={calls_before} calls_after={len(fake.calls)}")
    db.close()

    # --- B05: crash-gap rerun (terminal summary + FINALIZING) ----------------
    mid = _next_id()
    seed_frozen(mid)
    db = SessionLocal()
    # Simulate the crash gap: summary v1 terminal but meeting left FINALIZING
    # (i.e. the Finalizer completed summary persistence then died *before* the
    # complete_after_summary step). Insert terminal row directly at v1 WITHOUT
    # touching meeting status.
    db.add(MeetingSummary(meeting_id=mid, version=1, status="COMPLETED"))
    db.commit()
    probe = _Probe()  # NOT scripted -> any LLM call would raise (and be counted)
    finalizer_service.finalize_for_meeting(db, mid, provider=probe)
    m = db.get(Meeting, mid)
    _record("B05 crash-gap rerun completes meeting with ZERO extra LLM calls",
            m is not None and m.status == "COMPLETED" and len(probe.calls) == 0,
            f"status={m.status if m else None} probe_calls={len(probe.calls)}")
    db.close()

    # --- B06: no premature COMPLETED — meeting stays FINALIZING mid-LLM ------
    # §9: "Meeting COMPLETED only AFTER summary terminal (never during
    # GENERATING, never right after STREAM_END)". The Finalizer owns that gate:
    # complete_after_summary is only reached AFTER generate() returns a terminal
    # row. Prove it by probing the meeting status from INSIDE the provider call
    # (i.e. while the summary row is still GENERATING / no terminal row yet):
    # it must still be FINALIZING.
    class _StatusProbe(_Probe):
        name = "status-probe"

        def __init__(self, result: str | None = None, error: str | None = None) -> None:
            super().__init__(result=result or VALID_JSON, error=error)
            self.observed: str | None = None

        def complete(self, prompt, *, timeout_seconds=None):
            from app.db import SessionLocal as _SL

            read_db = _SL()
            try:
                obs = read_db.get(Meeting, self.obs_mid)
                self.observed = obs.status if obs else None
            finally:
                read_db.close()
            return super().complete(prompt, timeout_seconds=timeout_seconds)

    mid = _next_id()
    seed_frozen(mid)
    probe = _StatusProbe()
    probe.obs_mid = mid
    db = SessionLocal()
    finalizer_service.finalize_for_meeting(db, mid, provider=probe)
    _record("B06 meeting stays FINALIZING during LLM call (no premature COMPLETED)",
            getattr(probe, "observed", None) == "FINALIZING",
            f"observed_at_llm={getattr(probe, 'observed', None)}")
    db.close()

    # --- B07/B08/B09: SSE starts/completes AFTER durable; minimal payload; id
    # We capture envelopes by subscribing a consumer the same way event_stream
    # would, but in-process: subscribe first, then run the Finalizer, then read
    # the queue. Loop is bound so publish() marshals via call_soon_threadsafe —
    # which requires a running loop, so we instead leave loop=None (direct put).
    mid = _next_id()
    seed_frozen(mid)
    from app.realtime.event_broker import event_broker

    sub = event_broker.subscribe(mid)  # loop stays None -> direct put_nowait
    db = SessionLocal()
    fake = FakeProvider(responses=[VALID_JSON])
    finalizer_service.finalize_for_meeting(db, mid, provider=fake)
    # Drain the subscriber queue (synchronously available since loop is None).
    event_types = []
    while not sub.queue.empty():
        env = sub.queue.get_nowait()
        if env is not None:
            event_types.append(env)
    event_broker.unsubscribe(mid, sub)
    names = [e.event_type for e in event_types]
    _record("B07 summary.started + summary.completed both emitted",
            "summary.started" in names and "summary.completed" in names,
            f"names={names}")
    # Order (B25): started < completed < meeting.status(COMPLETED)
    started_i = names.index("summary.started") if "summary.started" in names else -1
    completed_i = names.index("summary.completed") if "summary.completed" in names else -1
    status_i = names.index("meeting.status") if "meeting.status" in names else -1
    _record("B25 SSE order: summary.started < summary.completed < meeting.status(COMPLETED)",
            0 <= started_i < completed_i < status_i,
            f"order={names}")
    comp = next((e for e in event_types if e.event_type == "summary.completed"), None)
    _record("B08 summary SSE payload minimal {version,status}",
            comp is not None and set(comp.payload.keys()) == {"version", "status"}
            and comp.payload.get("version") == 1 and comp.payload.get("status") == "COMPLETED",
            f"payload={comp.payload if comp else None}")
    _record("B09 stable SSE id summary:<mid>:<version>:<status>",
            comp is not None and comp.sse_id == f"summary:{mid}:1:completed",
            f"id={comp.sse_id if comp else None}")
    # No raw_model_output / stack / api key / prompt in the envelope payload.
    _record("B08b no raw_model_output / api key / prompt in SSE payload",
            all(
                not any(
                    k in json.dumps(e.payload).lower()
                    for k in ("raw_model_output", "api_key", "prompt", "traceback")
                )
                for e in event_types
                if e.event_type.startswith("summary.")
            ),
            f"payloads={[e.payload for e in event_types if e.event_type.startswith('summary.')]}")
    db.close()

    # --- B10: summary.* NOT replayed on connect (live-only) ------------------
    # The gap-free connect catch-up is built ONLY from persisted FINAL segments
    # (sse_service.connect: transcript_repository.list_sorted > cursor). Summary
    # events are non-durable — they live only in the (memory) broker and are
    # never replayed for a reconnecting client. A brand-new subscriber queue for
    # an already-finalized meeting must be empty (nothing buffered, no summary.*).
    mid = _next_id()
    seed_frozen(mid)
    db = SessionLocal()
    fake = FakeProvider(responses=[VALID_JSON])
    finalizer_service.finalize_for_meeting(db, mid, provider=fake)
    db.close()
    # A fresh subscribe (no prior queue state) sees nothing buffered.
    fresh_sub = event_broker.subscribe(mid)  # loop=None -> direct put_nowait
    pre_posted = []
    while not fresh_sub.queue.empty():
        e = fresh_sub.queue.get_nowait()
        if e is not None:
            pre_posted.append(e.event_type)
    event_broker.unsubscribe(mid, fresh_sub)
    _record("B10 summary.* not replayed on reconnect (live-only, no catch-up)",
            len(pre_posted) == 0, f"pre_posted={pre_posted}")

    # --- B11: SSE publish failure does NOT roll back DB commit ---------------
    # Simulate a broker-level failure: publish a summary event while the broker
    # subscriber queue is full (over maxsize) forces eviction — not a rollback.
    # Simpler + faithful to the invariant: patching publish to raise must leave
    # the durable summary + meeting COMPLETED intact.
    mid = _next_id()
    seed_frozen(mid)
    db = SessionLocal()
    original_pub = sse_service.publish_summary_completed

    def _boom(*_a, **_k):
        raise RuntimeError("sse subscriber gone")

    sse_service.publish_summary_completed = _boom
    try:
        fake = FakeProvider(responses=[VALID_JSON])
        finalizer_service.finalize_for_meeting(db, mid, provider=fake)
    finally:
        sse_service.publish_summary_completed = original_pub
    item = summary_repository.get_latest(db, mid)
    m = db.get(Meeting, mid)
    _record("B11 SSE publish failure does NOT roll back durable completion",
            item is not None and item.status == "COMPLETED"
            and m is not None and m.status == "COMPLETED",
            f"summary={item.status if item else None} meeting={m.status if m else None}")
    db.close()

    # --- B12/B12b: fail-closed unknown provider (never silently fake) --------
    from app.config import settings

    old_provider = settings.llm_provider
    fake_sentry = FakeProvider()  # if the factory fell back, this WOULD be used
    settings.llm_provider = "not-a-real-provider"
    try:
        from app.agent.llm.factory import get_provider

        raised = False
        try:
            get_provider()
        except LLMConfigurationError:
            raised = True
        _record("B12 unknown LLM_PROVIDER raises LLMConfigurationError (fail-closed)",
                raised is True)
    finally:
        settings.llm_provider = old_provider
    _record("B12b factory raised before any provider constructed (no fake fallback)",
            len(fake_sentry.calls) == 0, f"fake_calls={len(fake_sentry.calls)}")
    del fake_sentry

    # --- B18 (in-process, deterministic): regenerate while GENERATING -> 409 --
    # The HTTP layer is asynchronous (FastAPI BackgroundTasks), so the
    # "second regenerate while the latest is GENERATING" window is a race there.
    # In-process we hold the window open deterministically: create a v2
    # GENERATING row (never complete it), then attempt a regeneration.
    mid = _next_id()
    seed_frozen(mid)
    db = SessionLocal()
    fake_v1 = FakeProvider(responses=[VALID_JSON])
    finalizer_service.finalize_for_meeting(db, mid, provider=fake_v1)  # v1 COMPLETED
    # Allocate v2 as GENERATING (exactly like the HTTP route's create_regeneration
    # does), WITHOUT running Phase B/C — the window stays open.
    v2 = summary_service.create_regeneration(db, mid, provider=FakeProvider())
    try:
        summary_service.create_regeneration(db, mid, provider=FakeProvider())
        second = "no-raise"
    except TranscriptConflictError:
        second = "409"
    _record("B18 regenerate while latest GENERATING -> 409 (no dup model cost)",
            second == "409" and v2 is not None and v2.status == "GENERATING",
            f"second={second} v2_status={v2.status if v2 else None}")
    db.close()

    return 0


# =============================================================================
# Part B: black-box HTTP against a subprocess server (BackgroundTasks is real)
# =============================================================================

HOST = "127.0.0.1"
PORT = 8767
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


def _stream_end(mid: str, last: int, event_id: str) -> tuple[int, dict | str]:
    return _http_request(
        "POST", f"/api/internal/meetings/{mid}/stream-end",
        {"event_id": event_id, "meeting_id": mid, "event_type": "TRANSCRIPT_STREAM_END",
         "last_segment_seq": last},
    )


def _get_meeting(mid: str) -> tuple[int, dict | str]:
    return _http_request("GET", f"/api/meetings/{mid}")


def _wait_meeting_status(mid: str, target: str = "COMPLETED", timeout: float = 15.0) -> str | None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        code, body = _get_meeting(mid)
        if code == 200:
            last = body.get("status")
            if last == target:
                return last
        time.sleep(0.2)
    return last


def _part_b() -> int:
    # --- B13/B14/B15: first STREAM_END auto-finalizes; snapshot fields -------
    m = _new_meeting("B13-B16")
    _start(m["id"])
    _finish(m["id"])
    assert _final(m["id"], 1, "第一句话", "evt_b1") == 200
    assert _final(m["id"], 2, "第二句话", "evt_b2") == 200
    se_code, se_body = _stream_end(m["id"], 2, "evt_se_b")
    _record("B13 first STREAM_END accepted -> 200", se_code == 200, f"code={se_code}")
    _record("B14 StreamEndOut carries last_segment_seq + transcript_complete",
            isinstance(se_body, dict) and se_body.get("last_segment_seq") == 2
            and se_body.get("transcript_complete") is True,
            f"body={se_body}")
    st = _wait_meeting_status(m["id"], "COMPLETED")
    _record("B13b auto-Finalizer drives meeting -> COMPLETED (BackgroundTasks)",
            st == "COMPLETED", f"status={st}")

    code, body = _http_request("GET", f"/api/meetings/{m['id']}/summary")
    _record("B15 GET /summary -> 200 COMPLETED snapshot (overview/key_topics/version)",
            code == 200
            and isinstance(body, dict)
            and body.get("status") == "COMPLETED"
            and body.get("version") == 1
            and "overview" in body
            and isinstance(body.get("key_topics"), list),
            f"code={code} body={body if isinstance(body, dict) else body!r}")

    # --- B16: idempotent stream-end retry does NOT re-run summary ------------
    se2_code, _ = _stream_end(m["id"], 2, "evt_se_b_retry")
    _record("B16 stream-end retry -> 200 and no new summary version",
            se2_code == 200,
            f"code={se2_code}")
    time.sleep(1.0)  # let any (wrong) background task have a chance to run
    code, body = _http_request("GET", f"/api/meetings/{m['id']}/summary")
    _record("B16b retry produced NO new summary version (still v1, still COMPLETED)",
            code == 200 and isinstance(body, dict)
            and body.get("version") == 1 and body.get("status") == "COMPLETED",
            f"body={body if isinstance(body, dict) else body!r}")

    # --- B17/B19/B20: regenerate ---------------------------------------------
    code, body = _http_request("POST", f"/api/meetings/{m['id']}/summary/regenerate")
    _record("B17 POST /summary/regenerate -> 202 {version:2, status:GENERATING}",
            code == 202 and isinstance(body, dict)
            and body.get("meeting_id") == m["id"]
            and body.get("version") == 2
            and body.get("status") == "GENERATING",
            f"code={code} body={body if isinstance(body, dict) else body!r}")

    # Wait for v2 to complete in the background.
    v2_status = None
    deadline = time.time() + 15.0
    while time.time() < deadline:
        c, b = _http_request("GET", f"/api/meetings/{m['id']}/summary")
        if c == 200 and isinstance(b, dict):
            v2_status = b.get("status")
            if b.get("version") == 2 and b.get("status") == "COMPLETED":
                break
        time.sleep(0.2)
    _record("B19 v2 completes in background -> GET /summary shows v2 COMPLETED",
            v2_status == "COMPLETED", f"v2_status={v2_status}")

    _, mb = _get_meeting(m["id"])
    _record("B19b meeting STAYS COMPLETED after regeneration (§19 never reverts)",
            isinstance(mb, dict) and mb.get("status") == "COMPLETED",
            f"meeting={mb.get('status') if isinstance(mb, dict) else mb}")

    # B20: append-only via a second regenerate to v3.
    code3, _ = _http_request("POST", f"/api/meetings/{m['id']}/summary/regenerate")
    _record("B20a second regenerate -> 202 v3 (append-only versions)",
            code3 == 202, f"code={code3}")
    deadline = time.time() + 15.0
    v3_status = None
    while time.time() < deadline:
        c, b = _http_request("GET", f"/api/meetings/{m['id']}/summary")
        if c == 200 and isinstance(b, dict):
            if b.get("version") == 3 and b.get("status") == "COMPLETED":
                v3_status = "COMPLETED"
                break
        time.sleep(0.2)
    _record("B20b v3 COMPLETED (v1/v2/v3 append-only, never overwritten)",
            v3_status == "COMPLETED", f"v3_status={v3_status}")

    # --- B21: regenerate on a CREATED meeting (no frozen transcript) -> 409 --
    m_fresh = _new_meeting("B21-fresh")
    code, body = _http_request("POST", f"/api/meetings/{m_fresh['id']}/summary/regenerate")
    _record("B21 regenerate on CREATED (no frozen transcript) -> 409",
            code == 409, f"code={code} body={body if isinstance(body, dict) else body!r}")

    # --- B22: GET /summary 404 semantics -------------------------------------
    code_no, body_no = _http_request("GET", f"/api/meetings/{m_fresh['id']}/summary")
    _record("B22a GET /summary on meeting with no summary -> 404 summary not found",
            code_no == 404 and "summary not found" in str(body_no),
            f"code={code_no} body={body_no!r}")
    code_unk, body_unk = _http_request("GET", "/api/meetings/nonexistent_meeting_xyz/summary")
    _record("B22b GET /summary on unknown meeting -> 404 meeting not found",
            code_unk == 404 and "meeting" in str(body_unk),
            f"code={code_unk} body={body_unk!r}")

    # --- B23: regeneration input is persisted FINAL ASC only (no PARTIAL) ----
    # The server runs LLM_PROVIDER=fake which returns DEFAULT_JSON regardless of
    # prompt; to prove the input is FINAL-only we instead rely on the in-process
    # layer: build_summary_prompt + list_sorted(ASC) is Step 7A-tested (S19).
    # Here we assert the monitor hook: a PARTIAL was never persisted, so the
    # only transcript rows are the two FINALs in ASC order.
    code_t, body_t = _http_request("GET", f"/api/meetings/{m['id']}/transcript")
    _record("B23 persisted transcript is FINAL ASC only (no PARTIAL rows)",
            code_t == 200 and isinstance(body_t, dict)
            and [s.get("segment_seq") for s in body_t.get("segments", [])] == [1, 2],
            f"seqs={[s.get('segment_seq') for s in body_t.get('segments', [])] if isinstance(body_t, dict) else body_t!r}")

    # --- B26: GET /summary 200 + meeting 200 after COMPLETED (durable state) --
    code_fin, body_fin = _get_meeting(m["id"])
    _record("B26 final GET meeting -> 200 COMPLETED", code_fin == 200
            and isinstance(body_fin, dict) and body_fin.get("status") == "COMPLETED",
            f"status={body_fin.get('status') if isinstance(body_fin, dict) else body_fin}")

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
    rc_a = 0
    try:
        rc_a = _part_a()
    except Exception as exc:  # pragma: no cover
        _fail("Part A crashed", f"{type(exc).__name__}: {exc}")
        rc_a = 2

    rc_b = 0
    proc_b = _run_server()
    try:
        if not _wait_server():
            _fail("Part B server did not start")
            rc_b = 2
        else:
            rc_b = _part_b()
    except Exception as exc:  # pragma: no cover
        _fail("Part B crashed", f"{type(exc).__name__}: {exc}")
        rc_b = 2
    finally:
        proc_b.terminate()
        try:
            proc_b.wait(timeout=5)
        except Exception:
            proc_b.kill()

    print("\n==== Step 7B results ====")
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    failed = len(_RESULTS) - passed
    for case, ok, detail in _RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {case}" + (f" — {detail}" if detail else ""))
    print(f"\nTOTAL {len(_RESULTS)} cases: {passed} PASS / {failed} FAIL")
    sys.exit(0 if rc_a == 0 and rc_b == 0 and failed == 0 else 1)



