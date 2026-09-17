#!/usr/bin/env python
"""Mock Transcript Producer — a REAL external HTTP client (Step 4 §1, §12).

Simulates an upstream ASR service producing a transcript stream. It ONLY talks
to the Backend over HTTP, exactly like a future real ASR would:

  POST /api/internal/meetings/{meeting_id}/transcript-events   (PARTIAL/FINAL)
  POST /api/internal/meetings/{meeting_id}/stream-end           (TRANSCRIPT_STREAM_END)

It does NOT import any Backend internals (no app.services, no SQLAlchemy, no
live_utterance_cache, no Service calls). Swapping this script for real ASR must
not change the Backend contract.

It does NOT control the meeting lifecycle (no POST /meetings, /start, /finish).
Meeting lifecycle is driven by the orchestrator / test.

Usage:
  python scripts/mock_transcript_producer.py \
      --base-url http://127.0.0.1:8000 \
      --meeting-id meeting_xxx \
      --delay-ms 300

If --meeting-id is omitted, it creates one (CREATED, not started) for demo
convenience. When the meeting is already IN_PROGRESS the script just sends the
stream; it never starts/finishes the meeting itself.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


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


def _post_event(base: str, meeting_id: str, event: dict) -> None:
    code, body = _post(base, f"/api/internal/meetings/{meeting_id}/transcript-events", event)
    tag = event.get("status", event.get("event_type", "?"))
    print(f"  -> {tag:8s} seq={event.get('segment_seq','-')} http={code}")
    if code not in (200,):
        raise SystemExit(f"FATAL: transcript-event rejected http={code}: {body}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Mock ASR Transcript Producer (external HTTP client)")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--meeting-id", default=None)
    ap.add_argument("--delay-ms", type=int, default=300, help="simulated speaking cadence (NOT used for biz decisions)")
    args = ap.parse_args()

    base = args.base_url
    meeting_id = args.meeting_id

    # Optional: resolve a meeting if not given (never starts/finishes it).
    if meeting_id is None:
        code, body = _post(base, "/api/meetings", {"title": "Mock ASR 生成的会议"})
        if code != 201:
            raise SystemExit(f"FATAL: cannot create demo meeting http={code}: {body}")
        meeting_id = body["id"]
        print(f"[mock] created meeting {meeting_id} (CREATED; not started)")

    print(f"[mock] streaming into meeting {meeting_id} via {base}/api/internal ...")
    dl = args.delay_ms / 1000.0

    # --- simulated utterance 1 -------------------------------------------
    for text in ["我们今天", "我们今天讨论", "我们今天讨论会议助手MVP"]:
        _post_event(base, meeting_id, {
            "event_id": f"evt_u1_{int(time.time()*1000)}",
            "meeting_id": meeting_id,
            "utterance_id": "utt_mock_1",
            "speaker_id": "speaker_0",
            "text": text,
            "status": "PARTIAL",
            "start_ms": 0,
            "end_ms": 2000,
        })
        time.sleep(dl)
    _post_event(base, meeting_id, {
        "event_id": f"evt_u1_{int(time.time()*1000)}",
        "meeting_id": meeting_id,
        "utterance_id": "utt_mock_1",
        "segment_seq": 1,
        "speaker_id": "speaker_0",
        "text": "我们今天讨论会议助手MVP",
        "status": "FINAL",
        "start_ms": 0,
        "end_ms": 2000,
    })
    time.sleep(dl)

    # --- simulated utterance 2 -------------------------------------------
    for i, text in enumerate(["第一版", "第一版先做实时字幕"]):
        _post_event(base, meeting_id, {
            "event_id": f"evt_u2_{int(time.time()*1000)}",
            "meeting_id": meeting_id,
            "utterance_id": "utt_mock_2",
            "speaker_id": "speaker_1",
            "text": text,
            "status": "PARTIAL",
            "start_ms": 2000,
            "end_ms": 4000,
        })
        time.sleep(dl)
    _post_event(base, meeting_id, {
        "event_id": f"evt_u2_{int(time.time()*1000)}",
        "meeting_id": meeting_id,
        "utterance_id": "utt_mock_2",
        "segment_seq": 2,
        "speaker_id": "speaker_1",
        "text": "第一版先做实时字幕",
        "status": "FINAL",
        "start_ms": 2000,
        "end_ms": 4000,
    })
    time.sleep(dl)

    # --- simulated utterance 3 (the "last" one before finish) -------------
    _post_event(base, meeting_id, {
        "event_id": f"evt_u3_{int(time.time()*1000)}",
        "meeting_id": meeting_id,
        "utterance_id": "utt_mock_3",
        "speaker_id": "speaker_0",
        "text": "结束以后再生成会议纪要",
        "status": "PARTIAL",
        "start_ms": 4000,
        "end_ms": 6000,
    })
    time.sleep(dl)
    _post_event(base, meeting_id, {
        "event_id": f"evt_u3_{int(time.time()*1000)}",
        "meeting_id": meeting_id,
        "utterance_id": "utt_mock_3",
        "segment_seq": 3,
        "speaker_id": "speaker_0",
        "text": "结束以后再生成会议纪要",
        "status": "FINAL",
        "start_ms": 4000,
        "end_ms": 6000,
    })
    time.sleep(dl)

    # --- stream end ---
    code, body = _post(base, f"/api/internal/meetings/{meeting_id}/stream-end", {
        "event_id": f"evt_se_{int(time.time()*1000)}",
        "meeting_id": meeting_id,
        "event_type": "TRANSCRIPT_STREAM_END",
        "last_segment_seq": 3,
    })
    print(f"  -> STREAM_END http={code} body={body}")
    if code != 200:
        raise SystemExit(f"FATAL: stream-end rejected http={code}: {body}")

    print(f"[mock] DONE (meeting_id={meeting_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
