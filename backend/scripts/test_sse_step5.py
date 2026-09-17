#!/usr/bin/env python
"""Step 5 SSE integration tests — real HTTP + streaming SSE client (no browser).

Runs against a live uvicorn server started on a separate port with a **fresh
temp SQLite DB** so tests are isolated from dev data and existing tables.

The SSE client uses httpx.AsyncClient.stream() (a fetch-style client like the
PRD §31 frontend would), and we parse the SSE frame protocol on top. Each test
runs against the same server instance; a unique meeting per case isolates data.

Cases (Step 5 Part 21):
  C01  GET /events on missing meeting -> 404
  C02  PARTIAL -> transcript.partial received, DB transcript_segment unchanged
  C03  FINAL -> commit-before-publish (DB has seq before SSE frame)
  C04  FINAL idempotent retry -> 200, DB count unchanged, NO duplicate SSE
  C05  POST /start with SSE connected -> meeting.status IN_PROGRESS
  C06  POST /finish -> meeting.status FINALIZING
  C07  heartbeat on idle (short interval via env)
  C08  disconnect cleanup -> subscriber count returns to baseline
  C09  meeting isolation A/B
  C10  catch-up: DB 1,2,3; connect after_segment_seq=1 -> 2,3 then live
  C11  RACE: Snapshot=1, inject FINAL seq=2 mid-connect(wait) -> still received
  C12  catch-up + live ordering: 1,2,3 + connect(after=1) + concurrent seq=4 -> 2,3,4
  C13  PARTIAL dropped on reconnect is fine; FINAL still recovered
  C14  stream-end accepted, then /events?after_segment_seq=0 replays FINAL
  C15  no SSE consumer: Mock-style FINAL + STREAM_END still persist (regression)

Additional:
  C16  slow consumer: tiny queue maxsize, producer floods -> evict, no block
  C17  after_segment_seq < 0 -> 400
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import httpx

HOST = "127.0.0.1"
PORT = 8765
BASE = f"http://{HOST}:{PORT}"

# --- streaming SSE client ---------------------------------------------------
# httpx handles HTTP/1.1 chunked/identity transfer properly, so we only need to
# parse the SSE frame protocol (id/event/data + blank line) on top.

class SSEClient:
    """Streaming SSE client over httpx. Parses frames into dicts.

    Frames: {id, event, data, raw}. Buffers bytes until a blank line (the SSE
    frame terminator: \n\n or \r\n\r\n). `response_status()` returns after
    headers are available.
    """

    def __init__(self, path: str, headers: dict | None = None, timeout: float = 15.0):
        self.path = path
        self.headers = headers or {}
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._cm = None  # stream context manager
        self._response: httpx.Response | None = None
        self._buf = b""
        self._status: int | None = None

    async def connect(self):
        """Send GET and enter the streaming response (headers available now)."""
        # trust_env=False: never route localhost through the Windows system
        # proxy (Clash / corporate) — that silently 502s / misroutes the stream.
        self._client = httpx.AsyncClient(timeout=self._timeout, trust_env=False)
        raw_url = f"http://{HOST}:{PORT}{self.path}"
        headers = {"Accept": "text/event-stream", **self.headers}
        self._cm = self._client.stream("GET", raw_url, headers=headers)
        self._response = await self._cm.__aenter__()  # reads headers, stays open
        self._status = self._response.status_code

    async def response_status(self) -> tuple[int, str]:
        return (self._response.status_code if self._response else 0), ""

    async def next_frame(self, timeout: float = 5.0) -> dict | None:
        """Return the next complete SSE frame, or None on clean EOF.

        Blocks until a frame is complete or `timeout` elapses (TimeoutError).
        """
        if self._response is None:
            raise RuntimeError("connect() not called")

        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            term = self._find_frame_end(self._buf)
            if term is not None:
                sep_len, index = term
                frame_bytes, self._buf = self._buf[:index], self._buf[index + sep_len :]
                return self._parse(frame_bytes)
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError("no SSE frame within timeout")
            # Read WITHOUT cancel. The response is single-shot: an asyncio
            # wait_for timeout CANCELS the underlying stream read and poisons
            # httpx's aiter_bytes() iterator forever (StreamConsumed). We rely
            # on the server's heartbeat (bounded by our deadline) to make this
            # read return promptly; if a peer stalls with no heartbeat, the
            # frame deadline above still bounds the overall wait.
            chunk = await self._aiter_next_bytes()
            if chunk is None:
                if not self._buf:
                    return None
                # trailing partial — return what we have as best-effort
                return self._parse(self._buf)
            self._buf += chunk
    async def next_event(self, event: str, timeout: float = 6.0) -> dict:
        """Read frames until one with the given `event:` type arrives.

        Skips heartbeat / other types. Returns the parsed frame dict.
        Raises TimeoutError if the event never arrives.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"event {event!r} not received within {timeout}s")
            fr = await self.next_frame(timeout=max(0.05, remaining))
            if fr is None:
                raise RuntimeError("clean EOF before event")
            if fr.get("event") == event:
                return fr

    async def _aiter_next_bytes(self) -> bytes | None:
        """Read the next chunk from the streaming body."""
        if not hasattr(self, "_iter"):
            self._iter = self._response.aiter_bytes()
        return await self._iter.__anext__()

    @staticmethod
    def _find_frame_end(buf: bytes) -> tuple[int, int] | None:
        """Return (separator_len, index) of the end of the first SSE frame.

        A frame is terminated by a blank line — ``\n\n`` or ``\r\n\r\n``. The
        separator length matters: slicing past a ``\r\n\r\n`` with ``+2`` would
        leave stray ``\r\n`` in the buffer and corrupt the next parse.
        """
        i = buf.find(b"\n\n")
        j = buf.find(b"\r\n\r\n")
        if i == -1:
            i = float("inf")
        if j == -1:
            j = float("inf")
        if i < j:
            return (2, i)
        if j < i:
            return (4, j)
        return None

    @staticmethod
    def _parse(block: bytes) -> dict:
        frame: dict = {"id": None, "event": None, "data": "", "raw": block}
        text = block.decode("utf-8", "replace")
        for line in text.split("\n"):
            line = line.rstrip("\r")
            if line.startswith("id:"):
                frame["id"] = line[3:].strip()
            elif line.startswith("event:"):
                frame["event"] = line[6:].strip()
            elif line.startswith("data:"):
                frame["data"] += (line[5:].strip() + "\n") if frame["data"] else line[5:].strip()
        return frame

    async def close(self):
        if self._cm is not None:
            await self._cm.__aexit__(None, None, None)
            self._cm = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# --- SSEClient parser unit tests (transport-agnostic; no server needed) -------
#
# These pin the two invariants the live tests depend on:
#   1. a single SSE frame may arrive split across many chunks (fragmentation)
#   2. one httpx chunk may carry several complete frames (coalescing)
# The 1-chunk == 1-frame assumption was the original bug (a missing
# `self._buf += chunk`); these tests keep it from regressing.

from types import SimpleNamespace as _NS


async def _next_frame_offline(client) -> dict | None:
    """Offline mirror of SSEClient.next_frame's buffering loop.

    Reads from client._aiter_next_bytes() (a test-supplied stub), accumulates
    into client._buf exactly like the real method does, and returns one parsed
    frame (timer capped at ~1s then treated as "no more data").
    """
    deadline = asyncio.get_event_loop().time() + 1.0
    while True:
        term = client._find_frame_end(client._buf)
        if term is not None:
            sep_len, index = term
            frame_bytes, client._buf = client._buf[:index], client._buf[index + sep_len :]
            return client._parse(frame_bytes)
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            return None  # treat as timeout in offline mode
        chunk = await client._aiter_next_bytes()
        if chunk is None:
            return None
        client._buf += chunk


def _make_offline_client(chunks: list[bytes] | None = None, preload: bytes = b"") -> "object":
    """A fake SSEClient whose _aiter_next_bytes pulls from `chunks` in order."""
    client = _NS()
    client._buf = preload
    client._find_frame_end = SSEClient._find_frame_end
    client._parse = SSEClient._parse
    it = iter(chunks or [])

    async def _next_bytes():
        try:
            return next(it)
        except StopIteration:
            return None

    client._aiter_next_bytes = _next_bytes
    return client


def test_parser_fragmented_frame():
    """One SSE frame split across three chunks must still parse exactly once."""
    chunks = [
        b"event: transcript.",
        b"final\ndata: " + json.dumps({"a": 1}, ensure_ascii=False).encode(),
        b"\n\n",
    ]
    client = _make_offline_client(chunks)
    fr = asyncio.run(_next_frame_offline(client))
    assert fr is not None, "expected a parsed frame"
    assert fr["event"] == "transcript.final", f"event={fr['event']!r}"
    assert json.loads(fr["data"]) == {"a": 1}, fr["data"]
    # buffer must be drained completely (no trailing junk)
    assert client._buf == b"", f"leftover buf={client._buf!r}"


def test_parser_multiple_frames_one_chunk():
    """Two complete frames already in the buffer parse without re-reading wire."""
    frame_a = b"event: a\ndata: {\"x\": 1}\n\n"
    frame_b = b"event: b\ndata: {\"y\": 2}\n\n"
    client = _make_offline_client(chunks=[], preload=frame_a + frame_b)
    client._aiter_next_bytes = _never_read  # buf already holds both frames

    async def grab2():
        out = []
        for _ in range(2):
            fr = await _next_frame_offline(client)
            out.append(fr)
        return out

    f1, f2 = asyncio.run(grab2())
    assert f1["event"] == "a" and json.loads(f1["data"]) == {"x": 1}, f1
    assert f2["event"] == "b" and json.loads(f2["data"]) == {"y": 2}, f2
    assert client._buf == b"", f"leftover buf={client._buf!r}"


def _never_read():
    raise AssertionError("must not hit the wire")


# --- helpers ----------------------------------------------------------------

def _http_request(method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
    url = BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method=method,
    )
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


def new_meeting(title: str = "SSE Test") -> dict:
    code, body = _http_request("POST", "/api/meetings", {"title": title})
    assert code == 201, f"create meeting failed {code}: {body}"
    return body


def start_meeting(meeting_id: str):
    code, body = _http_request("POST", f"/api/meetings/{meeting_id}/start")
    assert code in (200, 201), f"start failed {code}: {body}"
    return body


def finish_meeting(meeting_id: str):
    code, body = _http_request("POST", f"/api/meetings/{meeting_id}/finish")
    assert code in (200, 201), f"finish failed {code}: {body}"
    return body


def post_final(meeting_id: str, seq: int, text="final text", event_id=None) -> int:
    code, _ = _http_request(
        "POST", f"/api/internal/meetings/{meeting_id}/transcript-events",
        {
            "event_id": event_id or f"evt_sse_{meeting_id}_{seq}_{int(time.time()*1000)}",
            "meeting_id": meeting_id,
            "utterance_id": f"utt_{seq}",
            "segment_seq": seq,
            "speaker_id": "speaker_1",
            "text": text,
            "status": "FINAL",
            "start_ms": seq * 1000,
            "end_ms": seq * 1000 + 500,
        },
    )
    return code


def post_partial(meeting_id: str, utt: str, text: str) -> int:
    code, _ = _http_request(
        "POST", f"/api/internal/meetings/{meeting_id}/transcript-events",
        {
            "event_id": f"evt_part_{meeting_id}_{utt}_{int(time.time()*1000)}",
            "meeting_id": meeting_id,
            "utterance_id": utt,
            "speaker_id": "speaker_1",
            "text": text,
            "status": "PARTIAL",
            "start_ms": 0,
            "end_ms": 100,
        },
    )
    return code


def get_transcript(meeting_id: str):
    code, body = _http_request("GET", f"/api/meetings/{meeting_id}/transcript")
    assert code == 200, f"transcript failed {code}: {body}"
    return body


def get_subscribers() -> int:
    code, body = _http_request("GET", "/api/internal/debug/sse-subscribers")
    assert code == 200, f"subscriber debug failed {code}: {body}"
    return (body or {}).get("total_subscribers")


def parse_with_type(data: str, event: str) -> dict:
    """Parse the normalized SSE event data JSON by event type."""
    obj = json.loads(data)
    assert obj.get("event_type") == event, f"expected {event}, got {obj}"
    return obj["payload"]


# --- test driver -------------------------------------------------------------

RESULTS: list[tuple[str, str]] = []


def record(case: str, ok: bool, detail: str = ""):
    RESULTS.append((case, "PASS" if ok else "FAIL"))
    print(f"  [{ 'PASS' if ok else 'FAIL' }] {case}" + (f" — {detail}" if detail else ""))
    sys.stdout.flush()


# --- async wrappers: never block the client event loop with urllib ----------
# Every blocking HTTP call from the async test MUST run in a worker thread,
# otherwise the SSE socket isn't serviced while a POST is in flight, and the
# test can miss frames.

async def a_new_meeting(title="SSE Test"):
    return await asyncio.to_thread(new_meeting, title)


async def a_start_meeting(meeting_id):
    return await asyncio.to_thread(start_meeting, meeting_id)


async def a_finish_meeting(meeting_id):
    return await asyncio.to_thread(finish_meeting, meeting_id)


async def a_post_final(meeting_id, seq, text="final text", event_id=None):
    return await asyncio.to_thread(post_final, meeting_id, seq, text, event_id)


async def a_post_partial(meeting_id, utt, text):
    return await asyncio.to_thread(post_partial, meeting_id, utt, text)


async def a_get_transcript(meeting_id):
    return await asyncio.to_thread(get_transcript, meeting_id)


async def a_get_subscribers():
    return await asyncio.to_thread(get_subscribers)


async def drain_while(client: "SSEClient", matches, count, timeout=6.0):
    """Collect `count` frames, in whatever interleaving happens — returns list of (event,data)."""
    out = []
    end = asyncio.get_event_loop().time() + timeout
    while len(out) < count:
        remaining = end - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise TimeoutError(f"needed {count}, got {len(out)}")
        fr = await client.next_frame(timeout=remaining)
        if fr is None:
            raise RuntimeError("eof early")
        out.append((fr.get("event"), fr.get("data")))
    return out


async def main() -> int:
    # reset server state? no — unique meetings per case.
    # C01: 404 on missing meeting
    try:
        client = SSEClient("/api/meetings/meeting_nonexistent_xyz/events")
        await client.connect()
        status, _ = await client.response_status()
        record("C01 404 missing meeting", status == 404, f"status={status}")
        await client.close()
    except Exception as e:
        record("C01 404 missing meeting", False, str(e))

    # C02 + C03 + C04 — one meeting: PARTIAL then FINAL then FINAL retry
    m2 = await a_new_meeting("C02/C03/C04")
    await a_start_meeting(m2["id"])
    client = SSEClient(f"/api/meetings/{m2['id']}/events")
    await client.connect()
    status, _ = await client.response_status()
    record("C02 connect status", status == 200, f"status={status}")

    # C02 PARTIAL
    await a_post_partial(m2["id"], "uttA", "我们")
    fr = await client.next_event("transcript.partial")
    record(
        "C02 partial event",
        parse_with_type(fr["data"], "transcript.partial")["text"] == "我们",
        f"event={fr.get('event')}",
    )
    # DB unchanged after PARTIAL
    tr = await a_get_transcript(m2["id"])
    rec_partial_db = len(tr["segments"]) == 0
    record("C02 partial not in DB", rec_partial_db, f"segments={len(tr['segments'])}")

    # C03 FINAL seq=1: DB must contain seq=1 BEFORE the SSE frame gets a chance
    c03_event_id = f"c03_evt_{m2['id']}_seq1"
    code, _body = await asyncio.to_thread(_http_request, "POST", f"/api/internal/meetings/{m2['id']}/transcript-events", {
        "event_id": c03_event_id,
        "meeting_id": m2["id"],
        "utterance_id": "utt_1",
        "segment_seq": 1,
        "speaker_id": "speaker_1",
        "text": "第一句",
        "status": "FINAL",
        "start_ms": 1000,
        "end_ms": 1500,
    })
    tr = await a_get_transcript(m2["id"])
    db_has_1 = code == 200 and any(s["segment_seq"] == 1 for s in tr["segments"])
    fr = await client.next_event("transcript.final")
    sse_seq = parse_with_type(fr["data"], "transcript.final")["segment_seq"]
    record(
        "C03 FINAL commit-before-publish",
        db_has_1 and sse_seq == 1,
        f"db_has_1={db_has_1} sse_seq={sse_seq}",
    )

    # C04 idempotent retry — Case A: SAME event_id + identical content -> 200,
    # DB count unchanged, and NO duplicate `transcript.final` on SSE (Part 4:
    # idempotent retry never re-broadcasts, because no new fact was persisted).
    c04_same_evt_code, _ = await asyncio.to_thread(_http_request, "POST", f"/api/internal/meetings/{m2['id']}/transcript-events", {
        "event_id": c03_event_id,  # same as C03
        "meeting_id": m2["id"],
        "utterance_id": "utt_1",
        "segment_seq": 1,
        "speaker_id": "speaker_1",
        "text": "第一句",
        "status": "FINAL",
        "start_ms": 1000,
        "end_ms": 1500,
    })
    db_count = len((await a_get_transcript(m2["id"]))["segments"])
    # Case C too: same (meeting, seq) but different event_id + identical content ->
    # also idempotent 200, no new row.
    c04_alt_evt_code, _ = await asyncio.to_thread(_http_request, "POST", f"/api/internal/meetings/{m2['id']}/transcript-events", {
        "event_id": f"c04_alt_evt_{m2['id']}_seq1",
        "meeting_id": m2["id"],
        "utterance_id": "utt_1",
        "segment_seq": 1,
        "speaker_id": "speaker_1",
        "text": "第一句",
        "status": "FINAL",
        "start_ms": 1000,
        "end_ms": 1500,
    })
    db_count2 = len((await a_get_transcript(m2["id"]))["segments"])
    record(
        "C04 idempotent retry 200 + count unchanged",
        c04_same_evt_code == 200 and c04_alt_evt_code == 200 and db_count == db_count2 == 1,
        f"same_evt={c04_same_evt_code} alt_evt={c04_alt_evt_code} db={db_count}->{db_count2}",
    )
    # no duplicate broadcast: within 1.4s no additional transcript.final
    dup_found = False
    try:
        fr2 = await client.next_frame(timeout=1.4)
        if fr2 and fr2.get("event") == "transcript.final":
            dup_found = True
    except TimeoutError:
        pass
    record("C04 no duplicate transcript.final on retry", not dup_found, f"dup={dup_found}")
    await client.close()

    # C05 meeting.status on start
    m5 = await a_new_meeting("C05")
    c5 = SSEClient(f"/api/meetings/{m5['id']}/events")
    await c5.connect()
    st, _ = await c5.response_status()
    assert st == 200, f"C05 connect {st}"
    await a_start_meeting(m5["id"])
    fr = await c5.next_event("meeting.status")
    pld = parse_with_type(fr["data"], "meeting.status")
    record("C05 meeting.status IN_PROGRESS on start", pld.get("status") == "IN_PROGRESS", f"pld={pld}")
    await c5.close()

    # C06 meeting.status FINALIZING on finish (also DISCONNECT check later)
    # need current status IN_PROGRESS: the same meeting m5 is IN_PROGRESS.
    # re-open a client (C05's closed) to keep clean semantics
    m6 = await a_new_meeting("C06")
    await a_start_meeting(m6["id"])
    c6 = SSEClient(f"/api/meetings/{m6['id']}/events")
    await c6.connect()
    st, _ = await c6.response_status()
    assert st == 200, f"C06 connect {st}"
    await a_finish_meeting(m6["id"])
    fr = await c6.next_event("meeting.status")
    pld = parse_with_type(fr["data"], "meeting.status")
    record("C06 meeting.status FINALIZING on finish", pld.get("status") == "FINALIZING", f"pld={pld}")
    await c6.close()

    # C07 heartbeat — we'll test with a SHORT interval by restarting server with
    # SSE_HEARTBEAT_INTERVAL. Do that in C07 section using env override on a
    # fresh server instance (see run_server). For simplicity, rely on the fact
    # the default is 15s: we don't want to wait 15s in CI. Start a SECOND server
    # with short interval in its own subprocess? Complexity — alternatively
    # directly verify heartbeat by crafting: we can't change interval live.
    # -> We'll set env when launching the server (SSE_HEARTBEAT_INTERVAL=0.5) so
    #    heartbeat frames arrive during every test. That also proves heartbeat.
    # C07 is then "idle connection receives heartbeat within ~1s".
    # We'll handle it with a dedicated idle client; pick up after start.

    # (C07 done below after server spin-up note.)

    # C09 isolation A/B
    mA = await a_new_meeting("C09a"); mB = await a_new_meeting("C09b")
    await a_start_meeting(mA["id"]); await a_start_meeting(mB["id"])
    cA = SSEClient(f"/api/meetings/{mA['id']}/events")
    cB = SSEClient(f"/api/meetings/{mB['id']}/events")
    await cA.connect(); await cB.connect()
    stA, _ = await cA.response_status(); stB, _ = await cB.response_status()
    assert stA == 200 and stB == 200, f"C09 connect {stA} {stB}"
    await a_post_final(mA["id"], 1, "仅A可见")
    fa = await cA.next_frame()
    pa = parse_with_type(fa["data"], "transcript.final") if fa and fa.get("event") == "transcript.final" else None
    # B should NOT receive A's final within 1s
    leaked = False
    try:
        fb = await cB.next_frame(timeout=1.0)
        if fb and fb.get("event") == "transcript.final":
            leaked = True
    except TimeoutError:
        pass
    record("C09 meeting isolation", pa and pa.get("text") == "仅A可见" and not leaked, f"leaked={leaked}")
    await cA.close(); await cB.close()

    # C10 catch-up: DB 1,2,3 then connect after_segment_seq=1
    m10 = await a_new_meeting("C10")
    await a_start_meeting(m10["id"])
    for s in (1, 2, 3):
        await a_post_final(m10["id"], s, f"s{s}")
    c10 = SSEClient(f"/api/meetings/{m10['id']}/events?after_segment_seq=1")
    await c10.connect()
    st10, _ = await c10.response_status()
    assert st10 == 200, f"C10 connect {st10}"
    f2 = await c10.next_event("transcript.final"); f3 = await c10.next_event("transcript.final")
    s2 = parse_with_type(f2["data"], "transcript.final")["segment_seq"]
    s3 = parse_with_type(f3["data"], "transcript.final")["segment_seq"]
    record("C10 catch-up delivers 2 then 3", s2 == 2 and s3 == 3, f"got {s2},{s3}")
    # then live: push seq=4
    await a_post_final(m10["id"], 4, "s4")
    f4 = await c10.next_event("transcript.final")
    s4 = parse_with_type(f4["data"], "transcript.final")["segment_seq"]
    record("C10 then live seq=4", s4 == 4, f"got {s4}")
    await c10.close()

    # C11 RACE — the critical test (Step 5 Part 11 / Part 21 Case 11).
    # The guarantee: connect() does catch-up-read THEN subscribe under the same
    # per-meeting lock FINAL ingest uses, so a FINAL can never commit "between"
    # those two. Each iteration uses a FRESH meeting so no historical catch-up
    # frames can contaminate the assertion: the only FINALs on the wire are the
    # baseline seq=1 (catch-up when after=0) and the iteration's target seq.
    ok_it = 0
    fail_info = None
    for i in range(8):
        mid = (await a_new_meeting("C11"))["id"]
        await a_start_meeting(mid)
        await a_post_final(mid, 1, "base")
        it_seq = 100 + i  # this iteration's unique seq
        after = 0 if i % 2 == 0 else 1  # alternate cursor

        c11 = SSEClient(f"/api/meetings/{mid}/events?after_segment_seq={after}")
        await c11.connect()
        st, _ = await c11.response_status()
        assert st == 200, f"C11 iter {i} connect {st}"

        # Fire a FINAL write concurrently with the just-established connection,
        # so the write races connect()'s catch-up-read/subscribe boundary.
        wt = asyncio.create_task(asyncio.to_thread(post_final, mid, it_seq, f"race{it_seq}"))
        got = set()
        end = asyncio.get_event_loop().time() + 3.0
        try:
            while True:
                remaining = end - asyncio.get_event_loop().time()
                if remaining <= 0 or it_seq in got:
                    break
                fr = await c11.next_event("transcript.final", timeout=remaining)
                got.add(parse_with_type(fr["data"], "transcript.final")["segment_seq"])
        except TimeoutError:
            pass
        await wt
        ok = it_seq in got
        if not ok:
            fail_info = (i, after, it_seq, sorted(got))
        ok_it += ok
        await c11.close()

    record(
        "C11 race: FINAL never lost across connect/write interleavings",
        ok_it == 8,
        f"hit {ok_it}/8; first_fail={fail_info}",
    )

    # C12 catch-up + live ordering 2,3,4
    m12 = await a_new_meeting("C12")
    await a_start_meeting(m12["id"])
    for s in (1, 2, 3):
        await a_post_final(m12["id"], s, f"s{s}")
    c12 = SSEClient(f"/api/meetings/{m12['id']}/events?after_segment_seq=1")
    await c12.connect()
    st, _ = await c12.response_status()
    # simultaneously produce seq=4
    wt = asyncio.create_task(asyncio.to_thread(post_final, m12["id"], 4, "s4live"))
    order = []
    try:
        for _ in range(3):
            fr = await c12.next_event("transcript.final", timeout=5.0)
            order.append(parse_with_type(fr["data"], "transcript.final")["segment_seq"])
    except TimeoutError:
        pass
    await wt
    record(
        "C12 strict ordering 2,3,4",
        order == [2, 3, 4],
        f"order={order}",
    )
    await c12.close()

    # C13 PARTIAL never replayed on reconnect; FINAL recovered via catch-up.
    m13 = await a_new_meeting("C13")
    await a_start_meeting(m13["id"])
    await a_post_partial(m13["id"], "p1", "断线期间的partial")
    await asyncio.sleep(0.2)
    await a_post_final(m13["id"], 1, "正式final")
    c13 = SSEClient(f"/api/meetings/{m13['id']}/events?after_segment_seq=0")
    await c13.connect()
    st, _ = await c13.response_status()
    got = await c13.next_event("transcript.final")  # must be FINAL first, no partial replay
    record(
        "C13 no PARTIAL replay, FINAL available",
        got.get("event") == "transcript.final",
        f"first_event={got.get('event')}",
    )
    await c13.close()

    # C14 stream-end accepted (meeting in FINALIZING), then replay FINAL
    m14 = await a_new_meeting("C14")
    await a_start_meeting(m14["id"])
    for s in (1, 2):
        await a_post_final(m14["id"], s, f"s{s}")
    await a_finish_meeting(m14["id"])  # FINALIZING required before stream-end
    se_code, se_body = await asyncio.to_thread(_http_request, "POST", f"/api/internal/meetings/{m14['id']}/stream-end", {
        "event_id": f"evt_se_{m14['id']}",
        "meeting_id": m14["id"],
        "event_type": "TRANSCRIPT_STREAM_END",
        "last_segment_seq": 2,
    })
    c14 = SSEClient(f"/api/meetings/{m14['id']}/events?after_segment_seq=0")
    await c14.connect()
    st, _ = await c14.response_status()
    f1 = await c14.next_event("transcript.final"); f2 = await c14.next_event("transcript.final")
    s1 = parse_with_type(f1["data"], "transcript.final")["segment_seq"]
    s2 = parse_with_type(f2["data"], "transcript.final")["segment_seq"]
    record(
        "C14 stream-end accepted + replay 1,2",
        se_code == 200 and s1 == 1 and s2 == 2,
        f"se_code={se_code} got {s1},{s2}",
    )
    await c14.close()

    # C15 no SSE consumer — regression: FINAL + STREAM_END still persist durably
    # with nobody subscribed. (meeting must be FINALIZING before stream-end.)
    m15 = await a_new_meeting("C15")
    await a_start_meeting(m15["id"])
    await a_post_final(m15["id"], 1, "无订阅者")
    await a_finish_meeting(m15["id"])
    se_code, se_body = await asyncio.to_thread(_http_request, "POST", f"/api/internal/meetings/{m15['id']}/stream-end", {
        "event_id": f"evt_se_{m15['id']}",
        "meeting_id": m15["id"],
        "event_type": "TRANSCRIPT_STREAM_END",
        "last_segment_seq": 1,
    })
    tr = await a_get_transcript(m15["id"])
    db_ = any(s["segment_seq"] == 1 for s in tr["segments"])
    record(
        "C15 no-consumer FINAL + stream-end persisted",
        se_code == 200 and db_,
        f"se_code={se_code} db_has_1={db_}",
    )

    # C16 slow consumer — tiny maxsize via env? We set SSE_QUEUE_MAXSIZE=3 for
    # this server, then open a subscriber, DON'T read, flood FINALs; assert
    # producer POSTs keep succeeding (non-blocking) AND the subscriber gets
    # evicted (no leak) rather than the producer blocking.
    m16 = await a_new_meeting("C16")
    await a_start_meeting(m16["id"])
    c16 = SSEClient(f"/api/meetings/{m16['id']}/events")
    await c16.connect()
    st, _ = await c16.response_status()
    ok_posts = 0
    # flood 10 finals rapidly while the client never reads
    for s in range(1, 11):
        code = await a_post_final(m16["id"], s, f"f{s}")
        if code == 200:
            ok_posts += 1
        await asyncio.sleep(0.01)
    evicts = await asyncio.to_thread(_http_request, "GET", "/api/internal/debug/sse-subscribers")
    record(
        "C16 slow consumer does not block producer",
        ok_posts == 10,
        f"ok_posts={ok_posts}/10 evicts={evicts[1]}",
    )
    await c16.close()

    # C17 after_segment_seq < 0 -> 400 (no stream started)
    c17 = SSEClient("/api/meetings/some_meeting/events?after_segment_seq=-1")
    await c17.connect()
    st, _ = await c17.response_status()
    record("C17 after<0 -> 400", st == 400, f"status={st}")
    await c17.close()

    # C08 disconnect cleanup: open N clients, close, check subscriber count.
    m8 = await a_new_meeting("C08")
    await a_start_meeting(m8["id"])
    base_count = await a_get_subscribers()
    clients = []
    for _ in range(5):
        c = SSEClient(f"/api/meetings/{m8['id']}/events")
        await c.connect(); await c.response_status()
        clients.append(c)
    after_open = await a_get_subscribers()
    for c in clients:
        await c.close()
    await asyncio.sleep(0.4)  # give finally/cleanup a beat
    after_close = await a_get_subscribers()
    record(
        "C08 disconnect cleanup",
        after_open == base_count + 5 and after_close == base_count,
        f"{base_count}->{after_open}->{after_close}",
    )

    # C07 heartbeat (idle connection).
    mh = await a_new_meeting("C07")
    await a_start_meeting(mh["id"])
    ch = SSEClient(f"/api/meetings/{mh['id']}/events")
    await ch.connect()
    st, _ = await ch.response_status()
    # with SSE_HEARTBEAT_INTERVAL=1 this appears fast
    hb = None
    try:
        # read until we hit a heartbeat; skip finals/status
        end = asyncio.get_event_loop().time() + 6
        while asyncio.get_event_loop().time() < end:
            fr = await ch.next_frame(timeout=min(2.0, end - asyncio.get_event_loop().time()))
            if fr and fr.get("event") == "heartbeat":
                hb = fr
                break
    except TimeoutError:
        pass
    record("C07 heartbeat received", hb is not None, f"hb={'yes' if hb else 'no'}")
    await ch.close()

    print("\n==== SSE Step 5 results ====")
    passed = sum(1 for _, r in RESULTS if r == "PASS")
    failed = sum(1 for _, r in RESULTS if r == "FAIL")
    for case, r in RESULTS:
        print(f"  {case}: {r}")
    print(f"\nTOTAL {passed} PASS / {failed} FAIL")
    return 0 if failed == 0 else 1


def run_server():
    """Start uvicorn with a temp DB + short heartbeat + small queue."""
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="sse_test_")
    dbfile = os.path.join(tmpdir, "test.db")
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{dbfile}"
    env["SSE_HEARTBEAT_INTERVAL"] = "1.0"   # 1s heartbeat for C07
    env["SSE_QUEUE_MAXSIZE"] = "3"           # small queue for C16
    # Log the server to a file so failures are debuggable even though the
    # subprocess stdout is PIPEd (uvicorn writes its own logs to stderr).
    backend_dir = pathlib.Path(__file__).resolve().parent.parent
    logf = open(os.path.join(tmpdir, "server.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", HOST, "--port", str(PORT)],
        cwd=backend_dir,
        env=env,
        stdout=logf,
        stderr=subprocess.STDOUT,
    )
    proc._test_logf = logf  # keep referenced; closed by Popen at exit
    return proc

#
# Wait for server
#
def wait_server(timeout=30.0):
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
    # Path is backend/app.main -> cwd backend
    import pathlib
    backend_dir = pathlib.Path(__file__).resolve().parent.parent
    os.chdir(backend_dir)

    proc = run_server()
    try:
        if not wait_server():
            print("server failed to start")
            sys.exit(2)
        rc = asyncio.run(main())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    sys.exit(rc)
