#!/usr/bin/env python
"""Mock ASR Transcript Producer — simulates the COLLABORATOR's output.

This script stands in for the upstream pipeline

    Audio / Tencent ASR  ->  normalized PARTIAL / FINAL

and speaks the EXTERNAL collaborator contract, NOT our internal one. It is a
REAL external HTTP client: it never imports Backend internals (no
app.services, no SQLAlchemy, no live cache).

Wire format (camelCase, no ``segmentSeq``)::

    {
      "type": "transcript.partial" | "transcript.final",
      "meetingId": "meeting_xxx",
      "asrSessionId": "mock-session-001",
      "sentenceId": 1,
      "speakerId": -1,            # -1 = speaker not yet identified
      "startMs": 1000,
      "endMs": 1800,
      "text": "我们今天",
      "provider": "tencent"
    }

Deliberately ABSENT, because they are the Transcript Domain's business:

  - ``segmentSeq``  — meeting-wide ordering; allocated by the Backend ingress
  - ``utteranceId`` — derived by the Backend from (asrSessionId, sentenceId)
  - ``eventId``     — derived by the Backend for FINAL idempotency

Target endpoint::

    POST /api/internal/meetings/{meeting_id}/asr-events

It does NOT control the meeting lifecycle (no create/start/finish). When
``--meeting-id`` is omitted it creates a meeting for demo convenience ONLY —
that is orchestrator glue, not part of the ASR contract.

Scenarios (``--scenario``) cover the ASR-0B acceptance surface:

    happy      正常流：多段 PARTIAL -> 每个最终 FINAL（默认）
    reconnect  ASR 重连：新 asrSessionId，sentenceId 从 1 重启
    duplicate  重复投递：同一 FINAL 重发（必须幂等 200）
    out-of-order 乱序：先发后句再发前句（segment_seq 按到达分配）
    errors     错误矩阵：故意发送非法 payload，断言被正确拒绝
    all        串联上述全部（不含 errors 的失败断言反转）

Usage::

    python scripts/mock_transcript_producer.py --meeting-id meeting_xxx --scenario happy
    python scripts/mock_transcript_producer.py --scenario all --delay-ms 100
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

# One ASR session for the whole mock run by default. The ``reconnect`` scenario
# deliberately starts a SECOND session id — that is exactly the multi-session
# timeline case frozen in protocols/transcript-format.md §5: a reconnect may
# restart sentenceId at 1 and the session-local clock at 0.
ASR_SESSION_ID = "mock-session-001"
RECONNECT_SESSION_ID = "mock-session-002"
PROVIDER = "tencent"

# (sentence_id, speaker_id, [(start_ms, end_ms, partial_text), ...], final_text)
# Each sentence emits several PARTIAL revisions, then one FINAL — exactly the
# shape the frontend must collapse into ONE line that updates in place.
SCRIPT: list[tuple[int, int, list[tuple[int, int, str]], str]] = [
    (
        1,
        0,
        [
            (1000, 1400, "我们今天"),
            (1000, 2600, "我们今天讨论会议助手"),
            (1000, 3200, "我们今天讨论会议助手的整体方案"),
        ],
        "我们今天讨论会议助手的整体方案。",
    ),
    (
        2,
        1,
        [
            (3600, 4200, "第一个版本"),
            (3600, 5600, "第一个版本先把实时字幕"),
        ],
        "第一个版本先把实时字幕和会议纪要跑通。",
    ),
    (
        3,
        0,
        [
            (6200, 6800, "上游的语音识别"),
        ],
        "上游的语音识别和说话人区分由其他同学负责。",
    ),
]

SCENARIOS = ("happy", "reconnect", "duplicate", "out-of-order", "errors", "all")

_PASS = 0
_FAIL = 0


def _check(name: str, ok: bool, detail: str = "") -> None:
    """Record an expectation for the asserting scenarios (duplicate/errors)."""
    global _PASS, _FAIL
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def _post(base: str, path: str, body: dict | None) -> tuple[int, dict]:
    url = base.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw) if raw else {}
            return resp.status, parsed
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"detail": raw}
        return e.code, parsed


def _get(base: str, path: str) -> tuple[int, dict]:
    req = urllib.request.Request(base.rstrip("/") + path, method="GET")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, (json.loads(raw) if raw else {})
        except json.JSONDecodeError:
            return e.code, {"detail": raw}


def _send_asr_event(base: str, meeting_id: str, event: dict, *, expect: int = 200) -> tuple[int, dict]:
    code, body = _post(base, f"/api/internal/meetings/{meeting_id}/asr-events", event)
    kind = "PARTIAL" if event["type"].endswith("partial") else "FINAL"
    print(f"  -> {kind:8s} sentence={event.get('sentenceId')} http={code}")
    if expect is not None and code != expect:
        raise SystemExit(f"FATAL: asr-event http={code} (expected {expect}): {body}")
    return code, body


def _base_event(meeting_id: str, sentence_id: int, speaker_id: int,
                start_ms: int, end_ms: int, text: str, kind: str,
                *, session_id: str = ASR_SESSION_ID) -> dict:
    """Build one external collaborator-format event (note: no segmentSeq)."""
    return {
        "type": f"transcript.{kind}",
        "meetingId": meeting_id,
        "asrSessionId": session_id,
        "sentenceId": sentence_id,
        "speakerId": speaker_id,
        "startMs": start_ms,
        "endMs": end_ms,
        "text": text,
        "provider": PROVIDER,
    }


def _emit_sentence(base: str, meeting_id: str, sentence: tuple, *, delay: float,
                   session_id: str = ASR_SESSION_ID) -> None:
    """PARTIALs first (speaker may still be unknown), then the FINAL."""
    sentence_id, speaker_id, partials, final_text = sentence
    for start_ms, end_ms, text in partials:
        _send_asr_event(
            base, meeting_id,
            _base_event(meeting_id, sentence_id, -1, start_ms, end_ms, text, "partial",
                        session_id=session_id),
        )
        time.sleep(delay)
    final_start = partials[0][0]
    final_end = partials[-1][1]
    _send_asr_event(
        base, meeting_id,
        _base_event(meeting_id, sentence_id, speaker_id, final_start, final_end,
                    final_text, "final", session_id=session_id),
    )
    time.sleep(delay)


def _snapshot(base: str, meeting_id: str) -> list[dict]:
    _, body = _get(base, f"/api/meetings/{meeting_id}/transcript")
    return body.get("segments", [])


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------

def scenario_happy(base: str, meeting_id: str, delay: float) -> None:
    print("[scenario] happy — 3 sentences, multi-PARTIAL then FINAL")
    for sentence in SCRIPT:
        _emit_sentence(base, meeting_id, sentence, delay=delay)


def scenario_reconnect(base: str, meeting_id: str, delay: float) -> None:
    """ASR reconnect: NEW session id, sentenceId restarts at 1, clock resets.

    The frozen contract (transcript-format §5): utterance_id is prefixed by
    asrSessionId so identities never collide, ordering is arrival-based, and the
    time offsets stay session-local (MVP does no rebase).
    """
    print("[scenario] reconnect — session A, then session B from scratch")
    _emit_sentence(base, meeting_id, SCRIPT[0], delay=delay, session_id=ASR_SESSION_ID)
    print("  ~~ ASR reconnect ~~")
    second = (
        1,
        1,
        [(0, 900, "重连之后")],
        "重连之后第一句重新编号。",
    )
    _emit_sentence(base, meeting_id, second, delay=delay, session_id=RECONNECT_SESSION_ID)


def scenario_duplicate(base: str, meeting_id: str, delay: float) -> None:
    """Re-delivery of an already-persisted FINAL must be idempotent (200)."""
    print("[scenario] duplicate — replay a persisted FINAL, expect idempotent 200")
    sentence_id, speaker_id, partials, final_text = SCRIPT[0]
    _emit_sentence(base, meeting_id, SCRIPT[0], delay=delay)

    before = len(_snapshot(base, meeting_id))
    final_start, final_end = partials[0][0], partials[-1][1]
    code, _ = _send_asr_event(
        base, meeting_id,
        _base_event(meeting_id, sentence_id, speaker_id, final_start, final_end,
                    final_text, "final"),
        expect=None,
    )
    after = len(_snapshot(base, meeting_id))
    _check("duplicate FINAL -> 200", code == 200, f"http={code}")
    _check("no extra segment persisted", before == after, f"{before} -> {after}")


def scenario_out_of_order(base: str, meeting_id: str, delay: float) -> None:
    """Out-of-order arrival: the LATER sentence is sent FIRST.

    segment_seq is allocated by arrival, never by sentenceId — so the persisted
    order must be the arrival order and the seq set must stay 1..N.
    """
    print("[scenario] out-of-order — send sentence 2 before sentence 1")
    _send_asr_event(base, meeting_id,
                    _base_event(meeting_id, 2, 1, 3600, 5600, SCRIPT[1][3], "final"))
    time.sleep(delay)
    _send_asr_event(base, meeting_id,
                    _base_event(meeting_id, 1, 0, 1000, 3200, SCRIPT[0][3], "final"))
    time.sleep(delay)

    segs = _snapshot(base, meeting_id)
    seqs = [s["segment_seq"] for s in segs]
    texts = [s["text"] for s in segs]
    _check("seqs are 1..N by arrival", seqs == [1, 2], f"seqs={seqs}")
    _check("sentence 2 arrived first", texts[0] == SCRIPT[1][3], f"texts={texts}")


def scenario_errors(base: str, meeting_id: str, delay: float) -> None:
    """Error matrix: malformed payloads must fail loudly, never be silently taken."""
    print("[scenario] errors — deliver rejected payloads, assert the rejections")

    def probe(name: str, event: dict, expect: int) -> None:
        code, body = _send_asr_event(base, meeting_id, event, expect=None)
        _check(name, code == expect, f"http={code} detail={str(body.get('detail'))[:70]}")
        time.sleep(delay)

    # Unknown field -> contract drift must be rejected (extra="forbid").
    drift = _base_event(meeting_id, 90, 0, 0, 100, "drift", "final")
    drift["segmentSeq"] = 1
    probe("unknown field (segmentSeq) -> 422", drift, 422)

    # Unknown event type.
    bad_type = _base_event(meeting_id, 91, 0, 0, 100, "x", "final")
    bad_type["type"] = "transcript.bogus"
    probe("unknown type -> 422", bad_type, 422)

    # endMs < startMs.
    probe("endMs < startMs -> 422",
          _base_event(meeting_id, 92, 0, 5000, 100, "x", "final"), 422)

    # speakerId below the allowed floor (-1 is legal, -2 is not).
    probe("speakerId=-2 -> 422",
          _base_event(meeting_id, 93, -2, 0, 100, "x", "final"), 422)

    # asrSessionId over the frozen 48-char bound.
    over = _base_event(meeting_id, 94, 0, 0, 100, "x", "final", session_id="a" * 49)
    probe("asrSessionId len=49 -> 422", over, 422)

    # path/payload meeting mismatch.
    mismatch = _base_event(meeting_id, 95, 0, 0, 100, "x", "final")
    mismatch["meetingId"] = "meeting_totally_other"
    probe("meetingId != path -> 400", mismatch, 400)

    # A FINAL whose content drifts from an already-persisted same sentence.
    _send_asr_event(base, meeting_id,
                    _base_event(meeting_id, 96, 0, 0, 100, "original", "final"))
    probe("same sentence, different content -> 409",
          _base_event(meeting_id, 96, 0, 0, 100, "TAMPERED", "final"), 409)

    # An utterance is CLOSED once FINAL: a later PARTIAL is rejected (ASR-0B §4).
    probe("PARTIAL after FINAL (same utterance) -> 409",
          _base_event(meeting_id, 96, 0, 0, 100, "late frame", "partial"), 409)

    # A non-existent meeting.
    code, _ = _post(base, "/api/internal/meetings/meeting_does_not_exist/asr-events",
                    _base_event("meeting_does_not_exist", 1, 0, 0, 100, "x", "final"))
    _check("unknown meeting -> 404", code == 404, f"http={code}")
    time.sleep(delay)


def _send_stream_end(base: str, meeting_id: str, *, last_seq: int | None = None) -> tuple[int, dict]:
    """Terminal signal.

    NOTE: this is the CURRENT INTERNAL MOCK terminal signal. It is NOT the
    agreed collaborator contract — the real STREAM_END sender/ownership is
    still PENDING (protocols/realtime-events.md §6.5). ``last_segment_seq`` is
    omitted when not supplied, exercising the ASR-0B server-derived path.
    """
    body = {
        "event_id": f"evt_se_{int(time.time()*1000)}",
        "meeting_id": meeting_id,
        "event_type": "TRANSCRIPT_STREAM_END",
    }
    if last_seq is not None:
        body["last_segment_seq"] = last_seq
    return _post(base, f"/api/internal/meetings/{meeting_id}/stream-end", body)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Mock ASR Transcript Producer (external collaborator contract)"
    )
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--meeting-id", default=None)
    ap.add_argument(
        "--scenario",
        choices=SCENARIOS,
        default="happy",
        help="which ASR-0B acceptance scenario to drive",
    )
    ap.add_argument(
        "--delay-ms",
        type=int,
        default=400,
        help="simulated speaking cadence (NOT used for any business decision)",
    )
    ap.add_argument(
        "--no-stream-end",
        action="store_true",
        help="skip the terminal STREAM_END (leave the meeting FINALIZING)",
    )
    args = ap.parse_args()

    base = args.base_url
    meeting_id = args.meeting_id
    delay = args.delay_ms / 1000.0

    # Orchestrator glue only — NOT part of the ASR contract.
    if meeting_id is None:
        code, body = _post(base, "/api/meetings", {"title": "Mock ASR 生成的会议"})
        if code != 201:
            raise SystemExit(f"FATAL: cannot create demo meeting http={code}: {body}")
        meeting_id = body["id"]
        print(f"[mock] created meeting {meeting_id} (CREATED; NOT started)")

    print(f"[mock] scenario={args.scenario} meeting={meeting_id} via {base}")

    if args.scenario in ("happy", "all"):
        scenario_happy(base, meeting_id, delay)
    if args.scenario in ("reconnect", "all"):
        scenario_reconnect(base, meeting_id, delay)
    if args.scenario in ("duplicate", "all"):
        scenario_duplicate(base, meeting_id, delay)
    if args.scenario in ("out-of-order", "all"):
        scenario_out_of_order(base, meeting_id, delay)

    # `errors` is a pure assertion scenario: run it FIRST on a fresh meeting or
    # it collides with the sentences above. Handled by the caller using a
    # dedicated meeting; here we still run it when explicitly selected.
    if args.scenario == "errors":
        scenario_errors(base, meeting_id, delay)

    segments = _snapshot(base, meeting_id)
    last_seq = segments[-1]["segment_seq"] if segments else 0
    print(f"[mock] snapshot: {len(segments)} persisted FINAL, last_segment_seq={last_seq}")
    print(f"[mock] seqs={[s['segment_seq'] for s in segments]}")
    print(f"[mock] utterances={[s['utterance_id'] for s in segments]}")

    if not args.no_stream_end:
        code, body = _send_stream_end(base, meeting_id, last_seq=last_seq)
        print(f"  -> STREAM_END (internal mock, NOT the agreed contract) http={code} body={body}")
        if code != 200:
            raise SystemExit(f"FATAL: stream-end rejected http={code}: {body}")

    if _FAIL:
        print(f"\n[mock] {_PASS} PASS / {_FAIL} FAIL")
        return 1
    if _PASS:
        print(f"\n[mock] {_PASS} PASS / 0 FAIL")
    print(f"[mock] DONE (meeting_id={meeting_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
