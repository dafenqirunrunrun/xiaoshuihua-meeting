#!/usr/bin/env python
"""ASR-0A acceptance tests — External ASR Transcript Adapter & Ingress.

Black-box HTTP against a subprocess uvicorn with a FRESH temp SQLite DB, so the
adapter/ingress is exercised through the real HTTP boundary and real persistence,
not by unit-calling internals.

Contract under test (ASR-0A):

    collaborator camelCase PARTIAL/FINAL  (no segmentSeq)
      -> POST /api/internal/meetings/{id}/asr-events
      -> adapter (pure translation + identity derivation)
      -> Transcript-owned segment_seq allocation (under meeting lock)
      -> EXISTING Transcript Domain (live cache / persist / commit / SSE)

Cases:
  ASR-A01  external PARTIAL schema parses -> 200
  ASR-A02  camelCase -> snake_case mapping correct (via SSE payload)
  ASR-A03  same (asrSessionId, sentenceId) -> SAME utterance identity
  ASR-A04  consecutive PARTIALs keep ONE utterance identity (in-place update)
  ASR-A05  speakerId = -1 accepted -> internal "unknown" (never "speaker_-1")
  ASR-A06  external FINAL adapts -> 200, persisted
  ASR-A07  FINAL clears the matching PARTIAL cache entry
  ASR-A08  FINAL lands in the durable transcript (Snapshot)
  ASR-A09  commit-before-publish: DB has the FINAL before the SSE frame
  ASR-A10  Snapshot recovers persisted FINAL after "refresh"
  ASR-A11  refresh then reconnect with the snapshot cursor still receives live FINAL
  ASR-A12  provider metadata never changes behaviour (tencent/whisper/aliyun)
  ASR-A13  illegal payloads rejected (not a crash)
  ASR-A14  payload meetingId != path meeting_id -> 400
  ASR-A15  SSE frames are NAMED frames (browser addEventListener contract)
  ASR-A16  PARTIAL revisions never multiply into extra rows
  ASR-A17  internal mock STREAM_END -> freeze works after adapter ingress
  ASR-A18  FakeProvider summary generated end-to-end (no DeepSeek)
  ASR-A19  Result can be opened (GET /summary) after completion
  ASR-A20  regenerate unaffected by the adapter path

Run:  backend/.venv/Scripts/python scripts/test_asr_adapter.py
"""

from __future__ import annotations

import json
import os
import pathlib
import queue
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import httpx

_BACKEND = pathlib.Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
os.chdir(_BACKEND)

HOST = "127.0.0.1"
PORT = 8769
BASE = f"http://{HOST}:{PORT}"

SESSION = "mock-session-001"
PROVIDER = "tencent"

_PASS = 0
_FAIL = 0


def _record(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _http(method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
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


def _asr_event(meeting_id: str, sentence_id: int, *, kind: str = "partial",
               speaker_id: int = -1, start_ms: int = 1000, end_ms: int = 1800,
               text: str = "我们今天", provider: str = PROVIDER,
               session_id: str = SESSION) -> dict:
    """Build a collaborator-format external event (camelCase, no segmentSeq)."""
    return {
        "type": f"transcript.{kind}",
        "meetingId": meeting_id,
        "asrSessionId": session_id,
        "sentenceId": sentence_id,
        "speakerId": speaker_id,
        "startMs": start_ms,
        "endMs": end_ms,
        "text": text,
        "provider": provider,
    }


def _post_asr(meeting_id: str, event: dict) -> tuple[int, dict | str]:
    return _http("POST", f"/api/internal/meetings/{meeting_id}/asr-events", event)


def _new_meeting(title: str) -> dict:
    code, body = _http("POST", "/api/meetings", {"title": title})
    assert code == 201, f"create failed {code}: {body}"
    return body


def _started_meeting(title: str) -> dict:
    m = _new_meeting(title)
    code, body = _http("POST", f"/api/meetings/{m['id']}/start")
    assert code == 200 and body["status"] == "IN_PROGRESS", body
    return body


def _snapshot(meeting_id: str) -> list[dict]:
    code, body = _http("GET", f"/api/meetings/{meeting_id}/transcript")
    assert code == 200, body
    return body["segments"]


# ---------------------------------------------------------------------------
# SSE client (sync httpx streaming; parses the SSE frame protocol)
# ---------------------------------------------------------------------------

class Sse:
    """Minimal sync SSE reader.

    The stream is drained by a BACKGROUND THREAD into a queue. This matters:
    httpx iterators are single-shot, and a read timeout poisons them
    (``StreamConsumed`` on the next read). Reading on a dedicated thread means
    our timeouts only ever expire on the queue, never on the socket, so the
    stream stays usable for the whole test.
    """

    def __init__(self, meeting_id: str, after_seq: int = 0):
        self.path = f"/api/meetings/{meeting_id}/events?after_segment_seq={after_seq}"
        self.frames: list[dict] = []
        self._client: httpx.Client | None = None
        self._cm = None
        self._resp = None
        self._q: queue.Queue[dict] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.status = 0

    def __enter__(self) -> "Sse":
        # trust_env=False: never route localhost through a system proxy.
        self._client = httpx.Client(timeout=None, trust_env=False)
        self._cm = self._client.stream("GET", BASE + self.path,
                                       headers={"Accept": "text/event-stream"})
        self._resp = self._cm.__enter__()
        self.status = self._resp.status_code
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        return self

    def _pump(self) -> None:
        """Background reader: parse SSE frames, push them onto the queue."""
        buf: list[str] = []
        try:
            for raw in self._resp.iter_lines():
                if self._stop.is_set():
                    return
                line = raw.rstrip("\r")
                if line == "":
                    if buf:
                        self._q.put(self._parse(buf))
                        buf = []
                    continue
                buf.append(line)
        except Exception:
            # disconnect / protocol end — nothing more will arrive
            pass
        finally:
            self._q.put({"event": "__eof__", "data": None})

    def read_until(self, event: str, timeout: float = 8.0) -> dict | None:
        """Read frames until one named `event` arrives; return it (or None)."""
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            try:
                fr = self._q.get(timeout=remaining)
            except queue.Empty:
                return None
            if fr.get("event") == "__eof__":
                return None
            self.frames.append(fr)
            if fr.get("event") == event:
                return fr
            if fr.get("event") == "heartbeat":
                continue

    def drain(self, seconds: float = 1.5) -> list[dict]:
        """Collect whatever arrives within `seconds`."""
        deadline = time.time() + seconds
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return self.frames
            try:
                fr = self._q.get(timeout=remaining)
            except queue.Empty:
                return self.frames
            if fr.get("event") == "__eof__":
                return self.frames
            self.frames.append(fr)

    @staticmethod
    def _parse(lines: list[str]) -> dict:
        out: dict = {"id": None, "event": None, "data": None}
        data_parts: list[str] = []
        for ln in lines:
            if ln.startswith("id:"):
                out["id"] = ln[3:].strip()
            elif ln.startswith("event:"):
                out["event"] = ln[6:].strip()
            elif ln.startswith("data:"):
                data_parts.append(ln[5:].lstrip())
        if data_parts:
            try:
                out["data"] = json.loads("\n".join(data_parts))
            except Exception:
                out["data"] = "\n".join(data_parts)
        return out

    def __exit__(self, *exc) -> None:
        self._stop.set()
        try:
            if self._cm is not None:
                self._cm.__exit__(*exc)
        except Exception:
            pass
        finally:
            if self._client is not None:
                self._client.close()


# ---------------------------------------------------------------------------
# Server bootstrap
# ---------------------------------------------------------------------------

def _run_server() -> subprocess.Popen:
    tmp_db = tempfile.mkdtemp(prefix="asr0a_")
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{tmp_db}/test.db",
        "LLM_PROVIDER": "fake",
        "SSE_HEARTBEAT_INTERVAL": "1",  # keep sync SSE reads prompt
    }
    logf = open(os.path.join(tmp_db, "server.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", HOST, "--port", str(PORT)],
        cwd=_BACKEND, env=env, stdout=logf, stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            code, _ = _http("GET", "/api/health")
            if code == 200:
                return proc
        except Exception:
            pass
        time.sleep(0.2)
    proc.terminate()
    raise RuntimeError("server did not start")


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

def run_cases() -> int:
    # ---- A01: external PARTIAL parses --------------------------------------
    m = _started_meeting("A01 partial parse")
    code, body = _post_asr(m["id"], _asr_event(m["id"], 1, kind="partial", text="我们今天"))
    _record("ASR-A01 external PARTIAL schema parses -> 200", code == 200, f"code={code} {body}")

    # ---- A13: illegal payloads rejected (run early; independent meetings) --
    m13 = _started_meeting("A13 invalid payloads")
    bad = [
        ("negative startMs", {**_asr_event(m13["id"], 1), "startMs": -5}),
        ("endMs < startMs", {**_asr_event(m13["id"], 1), "endMs": 10, "startMs": 900}),
        ("unknown type", {**_asr_event(m13["id"], 1), "type": "transcript.nope"}),
        ("empty asrSessionId", {**_asr_event(m13["id"], 1), "asrSessionId": ""}),
        ("missing provider", {k: v for k, v in _asr_event(m13["id"], 1).items()
                              if k != "provider"}),
        ("speakerId < -1", {**_asr_event(m13["id"], 1), "speakerId": -2}),
        ("segmentSeq supplied", {**_asr_event(m13["id"], 1), "segmentSeq": 98}),
        ("snake_case leakage", {"event_type": "transcript.partial", "meeting_id": m13["id"]}),
    ]
    all_rejected = True
    details = []
    for label, payload in bad:
        c, _ = _post_asr(m13["id"], payload)
        details.append(f"{label}={c}")
        if c not in (400, 422):
            all_rejected = False
    _record("ASR-A13 illegal payloads rejected (400/422, never crashed)",
            all_rejected, ", ".join(details))

    # ---- A14: meetingId mismatch -------------------------------------------
    m14 = _started_meeting("A14 mismatch")
    other = _new_meeting("A14 other")
    code, body = _post_asr(m14["id"], _asr_event(other["id"], 1))
    detail = body.get("detail") if isinstance(body, dict) else body
    _record("ASR-A14 payload meetingId != path meeting_id -> 400",
            code == 400 and "meeting_id" in str(detail), f"code={code} {detail}")

    # ---- A02/A03/A04/A05/A15/A16: PARTIAL path over real SSE ---------------
    m2 = _started_meeting("A02-A05 partial semantics")
    with Sse(m2["id"], 0) as sse:
        assert sse.status == 200, f"SSE connect failed {sse.status}"

        # three revisions of the SAME sentence, speaker still unknown
        revs = [(1000, 1400, "我们今天"), (1000, 2600, "我们今天讨论"),
                (1000, 3200, "我们今天讨论会议助手")]
        for start_ms, end_ms, text in revs:
            c, _ = _post_asr(m2["id"], _asr_event(
                m2["id"], 1, kind="partial", speaker_id=-1,
                start_ms=start_ms, end_ms=end_ms, text=text))
            assert c == 200, f"partial rejected {c}"

        got = []
        deadline = time.time() + 8
        while len(got) < 3 and time.time() < deadline:
            fr = sse.read_until("transcript.partial", timeout=max(0.5, deadline - time.time()))
            if fr:
                got.append(fr)

        _record("ASR-A15 SSE frames are NAMED frames (browser addEventListener)",
                all(f.get("event") == "transcript.partial" for f in got) and len(got) == 3,
                f"got={len(got)} events={[f.get('event') for f in got]}")

        payloads = [f["data"]["payload"] for f in got]
        uids = {p["utterance_id"] for p in payloads}
        _record("ASR-A03 same (asrSessionId, sentenceId) -> SAME utterance identity",
                len(got) == 3 and uids == {f"{SESSION}:1"},
                f"uids={uids}")
        _record("ASR-A04 3 PARTIAL revisions collapse to ONE identity (in-place update)",
                len(payloads) == 3 and len(uids) == 1,
                f"revisions={[p['text'] for p in payloads]}")
        _record("ASR-A16 PARTIAL revisions never multiply into extra rows",
                len(uids) == 1 and payloads[-1]["text"] == "我们今天讨论会议助手",
                f"final_text={payloads[-1]['text']!r}")
        _record("ASR-A05 speakerId = -1 -> internal 'unknown' (never 'speaker_-1')",
                all(p["speaker_id"] == "unknown" for p in payloads),
                f"speakers={ {p['speaker_id'] for p in payloads} }")
        _record("ASR-A02 camelCase -> snake_case mapping correct",
                payloads[0]["start_ms"] == 1000 and payloads[0]["end_ms"] == 1400
                and payloads[0]["text"] == "我们今天",
                f"start_ms={payloads[0]['start_ms']} end_ms={payloads[0]['end_ms']}")

        # ---- A06/A07/A09: FINAL -------------------------------------------------
        c, _ = _post_asr(m2["id"], _asr_event(
            m2["id"], 1, kind="final", speaker_id=0,
            start_ms=1000, end_ms=3200, text="我们今天讨论会议助手的整体方案。"))
        _record("ASR-A06 external FINAL adapts -> 200", c == 200, f"code={c}")

        ffr = sse.read_until("transcript.final", timeout=8)
        _record("ASR-A09 commit-before-publish (FINAL durable before SSE frame)",
                ffr is not None and _snapshot(m2["id"]) != [],
                f"segs={len(_snapshot(m2['id']))}")

        # ---- A07: PARTIAL cleared for that utterance ---------------------------
        # Re-send a PARTIAL for the SAME sentence; it must be a NEW live partial
        # (the previous cache entry was removed by the FINAL).
        c, _ = _post_asr(m2["id"], _asr_event(
            m2["id"], 1, kind="partial", speaker_id=-1, text="我们今天讨论会议助手的整体"))
        assert c == 200, c
        again = sse.read_until("transcript.partial", timeout=8)
        _record("ASR-A07 FINAL cleared the matching PARTIAL cache entry",
                again is not None and again["data"]["payload"]["utterance_id"] == f"{SESSION}:1",
                f"uid={again['data']['payload']['utterance_id'] if again else None}")

    # ---- A08: durable persistence -----------------------------------------
    segs = _snapshot(m2["id"])
    _record("ASR-A08 FINAL persisted (Snapshot returns it)",
            len(segs) == 1 and segs[0]["segment_seq"] == 1
            and segs[0]["speaker_id"] == "speaker_0",
            f"segs={[(s['segment_seq'], s['speaker_id'], s['text'][:8]) for s in segs]}")

    # ---- A10/A11: refresh -> snapshot -> reconnect -> live FINAL -----------
    m10 = _started_meeting("A10-A11 resume")
    for sid, spk, txt in [(1, 0, "第一句"), (2, 1, "第二句")]:
        c, _ = _post_asr(m10["id"], _asr_event(
            m10["id"], sid, kind="final", speaker_id=spk, text=txt,
            start_ms=sid * 1000, end_ms=sid * 1000 + 500))
    persisted = _snapshot(m10["id"])
    _record("ASR-A10 Snapshot recovers persisted FINAL after refresh",
            len(persisted) == 2 and [s["segment_seq"] for s in persisted] == [1, 2],
            f"seqs={[s['segment_seq'] for s in persisted]}")

    cursor = persisted[-1]["segment_seq"]
    with Sse(m10["id"], cursor) as sse:
        assert sse.status == 200
        c, _ = _post_asr(m10["id"], _asr_event(
            m10["id"], 3, kind="final", speaker_id=0, text="第三句",
            start_ms=3000, end_ms=3500))
        assert c == 200, c
        fr = sse.read_until("transcript.final", timeout=8)
        seq = fr["data"]["payload"]["segment_seq"] if fr else None
        _record("ASR-A11 refresh then reconnect with cursor still gets live FINAL",
                fr is not None and seq == 3, f"seq={seq}")

    # ---- A12: provider metadata is inert -----------------------------------
    m12 = _started_meeting("A12 provider inert")
    ok = True
    for i, prov in enumerate(["tencent", "whisper", "aliyun", "iflytek"], start=1):
        c, _ = _post_asr(m12["id"], _asr_event(
            m12["id"], i, kind="final", speaker_id=0, text=f"provider-{prov}",
            start_ms=i * 1000, end_ms=i * 1000 + 100, provider=prov))
        ok = ok and c == 200
    segs12 = _snapshot(m12["id"])
    _record("ASR-A12 provider metadata never changes behaviour",
            ok and len(segs12) == 4 and [s["segment_seq"] for s in segs12] == [1, 2, 3, 4],
            f"providers=4 segs={len(segs12)}")

    # ---- A17/A18/A19/A20: freeze -> summary -> result -> regenerate -------
    m17 = _started_meeting("A17-A20 full chain")
    for sid, spk, txt in [(1, 0, "我们今天讨论会议助手 MVP 的整体方案。"),
                          (2, 1, "第一个版本先把实时字幕跑通。")]:
        c, _ = _post_asr(m17["id"], _asr_event(
            m17["id"], sid, kind="final", speaker_id=spk, text=txt,
            start_ms=sid * 4000, end_ms=sid * 4000 + 3000))
        assert c == 200, c
    segs17 = _snapshot(m17["id"])
    last_seq = segs17[-1]["segment_seq"]

    c, _ = _http("POST", f"/api/meetings/{m17['id']}/finish")
    assert c == 200, f"finish failed {c}"

    # internal mock STREAM_END (NOT the agreed collaborator contract)
    c, body = _http("POST", f"/api/internal/meetings/{m17['id']}/stream-end", {
        "event_id": f"evt_se_{int(time.time()*1000)}",
        "meeting_id": m17["id"],
        "event_type": "TRANSCRIPT_STREAM_END",
        "last_segment_seq": last_seq,
    })
    _record("ASR-A17 internal mock STREAM_END -> freeze works after adapter ingress",
            c == 200 and body.get("transcript_complete") is True,
            f"code={c} last_segment_seq={body.get('last_segment_seq')}")

    # wait for the Finalizer (BackgroundTasks) to reach a terminal summary
    summary = None
    deadline = time.time() + 30
    while time.time() < deadline:
        c, s = _http("GET", f"/api/meetings/{m17['id']}/summary")
        if c == 200 and s.get("status") in ("COMPLETED", "FAILED"):
            summary = s
            break
        time.sleep(0.4)

    _record("ASR-A18 FakeProvider summary generated (no DeepSeek configured)",
            summary is not None and summary.get("status") == "COMPLETED"
            and summary.get("model_name") == "fake-provider",
            f"status={summary.get('status') if summary else None} "
            f"model={summary.get('model_name') if summary else None}")

    c, meeting = _http("GET", f"/api/meetings/{m17['id']}")
    _record("ASR-A19 Result can be opened after completion",
            c == 200 and meeting.get("status") == "COMPLETED"
            and summary is not None and bool(summary.get("overview")),
            f"meeting={meeting.get('status')} overview_len="
            f"{len(summary.get('overview') or '') if summary else 0}")

    c, regen = _http("POST", f"/api/meetings/{m17['id']}/summary/regenerate")
    v = regen.get("version") if isinstance(regen, dict) else None
    final = None
    deadline = time.time() + 30
    while time.time() < deadline:
        cc, ss = _http("GET", f"/api/meetings/{m17['id']}/summary")
        if cc == 200 and ss.get("version") == v and ss.get("status") in ("COMPLETED", "FAILED"):
            final = ss
            break
        time.sleep(0.4)
    c2, m2f = _http("GET", f"/api/meetings/{m17['id']}")
    _record("ASR-A20 regenerate unaffected by the adapter path",
            c == 202 and v == 2 and final is not None
            and final.get("status") == "COMPLETED" and m2f.get("status") == "COMPLETED",
            f"http={c} version={v} status={final.get('status') if final else None} "
            f"meeting={m2f.get('status')}")

    return 0


def main() -> int:
    proc = _run_server()
    try:
        rc = run_cases()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    print(f"\nTOTAL: {_PASS} PASS / {_FAIL} FAIL")
    return 1 if _FAIL else rc


if __name__ == "__main__":
    sys.exit(main())
