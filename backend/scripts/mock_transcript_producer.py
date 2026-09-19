#!/usr/bin/env python
"""Mock ASR Transcript Producer — simulates the COLLABORATOR's output (ASR-0A §11).

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

Usage::

    python scripts/mock_transcript_producer.py --meeting-id meeting_xxx --delay-ms 400
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

# One ASR session for the whole mock run. A real producer would start a new
# session id after an ASR reconnect; that timestamp-reset semantics is still
# PENDING collaborator confirmation and is intentionally NOT modelled here.
ASR_SESSION_ID = "mock-session-001"
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


def _send_asr_event(base: str, meeting_id: str, event: dict) -> None:
    code, body = _post(base, f"/api/internal/meetings/{meeting_id}/asr-events", event)
    kind = "PARTIAL" if event["type"].endswith("partial") else "FINAL"
    print(f"  -> {kind:8s} sentence={event['sentenceId']} http={code}")
    if code != 200:
        raise SystemExit(f"FATAL: asr-event rejected http={code}: {body}")


def _base_event(meeting_id: str, sentence_id: int, speaker_id: int,
                start_ms: int, end_ms: int, text: str, kind: str) -> dict:
    """Build one external collaborator-format event (note: no segmentSeq)."""
    return {
        "type": f"transcript.{kind}",
        "meetingId": meeting_id,
        "asrSessionId": ASR_SESSION_ID,
        "sentenceId": sentence_id,
        "speakerId": speaker_id,
        "startMs": start_ms,
        "endMs": end_ms,
        "text": text,
        "provider": PROVIDER,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Mock ASR Transcript Producer (external collaborator contract)"
    )
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--meeting-id", default=None)
    ap.add_argument(
        "--delay-ms",
        type=int,
        default=400,
        help="simulated speaking cadence (NOT used for any business decision)",
    )
    args = ap.parse_args()

    base = args.base_url
    meeting_id = args.meeting_id

    # Orchestrator glue only — NOT part of the ASR contract.
    if meeting_id is None:
        code, body = _post(base, "/api/meetings", {"title": "Mock ASR 生成的会议"})
        if code != 201:
            raise SystemExit(f"FATAL: cannot create demo meeting http={code}: {body}")
        meeting_id = body["id"]
        print(f"[mock] created meeting {meeting_id} (CREATED; NOT started)")

    print(
        f"[mock] streaming into meeting {meeting_id} via {base}/api/internal/.../asr-events\n"
        f"[mock] asrSessionId={ASR_SESSION_ID} provider={PROVIDER}"
    )
    dl = args.delay_ms / 1000.0

    for sentence_id, speaker_id, partials, final_text in SCRIPT:
        # PARTIALs first: speaker may still be unknown (-1) mid-sentence.
        for start_ms, end_ms, text in partials:
            _send_asr_event(
                base, meeting_id,
                _base_event(meeting_id, sentence_id, -1, start_ms, end_ms, text, "partial"),
            )
            time.sleep(dl)

        # FINAL: speaker resolved, same sentenceId -> same utterance identity.
        final_start = partials[0][0]
        final_end = partials[-1][1]
        _send_asr_event(
            base, meeting_id,
            _base_event(meeting_id, sentence_id, speaker_id, final_start, final_end,
                        final_text, "final"),
        )
        time.sleep(dl)

    # NOTE: stream-end below is the CURRENT INTERNAL MOCK terminal signal.
    # It is NOT the agreed collaborator contract — the real STREAM_END payload,
    # sender and ownership (incl. last_segment_seq) are PENDING confirmation.
    # We read the ACTUAL transcript ordering from the Snapshot rather than
    # assuming it: segment_seq is Transcript-owned and we never supply it.
    req = urllib.request.Request(base.rstrip("/") + f"/api/meetings/{meeting_id}/transcript")
    with urllib.request.urlopen(req) as resp:
        snapshot = json.loads(resp.read().decode("utf-8"))
    segments = snapshot.get("segments", [])
    last_seq = segments[-1]["segment_seq"] if segments else 0
    print(f"[mock] snapshot: {len(segments)} persisted FINAL, last_segment_seq={last_seq}")

    code, body = _post(base, f"/api/internal/meetings/{meeting_id}/stream-end", {
        "event_id": f"evt_se_{int(time.time()*1000)}",
        "meeting_id": meeting_id,
        "event_type": "TRANSCRIPT_STREAM_END",
        "last_segment_seq": last_seq,
    })
    print(f"  -> STREAM_END (internal mock, NOT the agreed contract) http={code} body={body}")
    if code != 200:
        raise SystemExit(f"FATAL: stream-end rejected http={code}: {body}")

    print(f"[mock] DONE (meeting_id={meeting_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
