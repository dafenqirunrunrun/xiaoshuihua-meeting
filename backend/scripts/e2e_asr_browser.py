#!/usr/bin/env python
"""ASR-0A browser E2E over CDP — verifies the REAL React UI, not just the API.

The whole point of ASR-0A is that the collaborator's normalized format drives the
existing realtime chain end to end. API-level tests cannot prove the browser
actually receives the NAMED SSE frames (regression we already paid for once:
named frames do not fire `onmessage`, so a hook using addEventListener('message')
silently receives nothing). This script drives a real Chrome page and asserts on
the DOM.

Flow:
  create meeting -> start -> mock ASR PARTIALs (same sentence) -> FINAL
  -> Snapshot/Resume -> second sentence -> finish -> internal mock STREAM_END
  -> freeze -> FakeProvider summary -> COMPLETED -> Result -> regenerate

Assertions (ASR-A04 / A11 / A15 / A16 in a real browser):
  - consecutive PARTIALs of one sentence render as exactly ONE live line
    (in-place replacement, never 3 stacked rows)
  - the PARTIAL line is replaced by exactly ONE FINAL line
  - speakerId=-1 renders without a "speaker_-1" label
  - refresh mid-meeting restores FINAL from Snapshot and keeps streaming
  - the Result page reaches a real persisted summary, then regenerates to v2

Run:  backend/.venv/Scripts/python scripts/e2e_asr_browser.py
Requires: backend :8000, vite :5173, Chrome with --remote-debugging-port=9222.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.error
import urllib.request

import websockets

CDP = "http://127.0.0.1:9222"
APP = "http://127.0.0.1:5173"
BACKEND = "http://127.0.0.1:8000"

SESSION = "mock-session-001"

_PASS = 0
_FAIL = 0


def _record(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


# --------------------------------------------------------------------------
# Plain HTTP against the backend
# --------------------------------------------------------------------------

def _http(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BACKEND + path, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}


def _asr(meeting_id: str, sentence_id: int, *, kind: str, speaker_id: int = -1,
         start_ms: int = 1000, end_ms: int = 1800, text: str = "") -> dict:
    return {
        "type": f"transcript.{kind}",
        "meetingId": meeting_id,
        "asrSessionId": SESSION,
        "sentenceId": sentence_id,
        "speakerId": speaker_id,
        "startMs": start_ms,
        "endMs": end_ms,
        "text": text,
        "provider": "tencent",
    }


# --------------------------------------------------------------------------
# CDP driver
# --------------------------------------------------------------------------

class Page:
    def __init__(self) -> None:
        self.ws: websockets.WebSocketClientProtocol | None = None
        self._id = 0

    async def connect(self) -> None:
        with urllib.request.urlopen(CDP + "/json/list", timeout=10) as r:
            targets = json.load(r)
        page = next((t for t in targets if t.get("type") == "page"), None)
        if page is None:
            raise RuntimeError("no page target; open a tab in the debug Chrome")
        self.ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024)

    async def send(self, method: str, **params) -> dict:
        self._id += 1
        mid = self._id
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=30))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def eval(self, expr: str):
        res = await self.send(
            "Runtime.evaluate",
            expression=expr,
            returnByValue=True,
            awaitPromise=True,
        )
        return res.get("result", {}).get("value")

    async def goto(self, url: str) -> None:
        await self.send("Page.enable")
        await self.send("Page.navigate", url=url)
        await asyncio.sleep(0.5)

    async def wait_for(self, expr: str, timeout: float = 20.0, label: str = "") -> bool:
        """Poll an expression until it is truthy."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if await self.eval(expr):
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.35)
        return False

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()


# --- DOM probes (kept as JS expressions so assertions read the REAL UI) ----

# Final transcript lines: li.transcript-segment WITHOUT the --partial modifier.
JS_FINAL_COUNT = "document.querySelectorAll('li.transcript-segment:not(.transcript-segment--partial)').length"
JS_PARTIAL_COUNT = "document.querySelectorAll('li.transcript-segment--partial').length"
JS_FINAL_TEXTS = (
    "Array.from(document.querySelectorAll("
    "'li.transcript-segment:not(.transcript-segment--partial) .transcript-segment__text'))"
    ".map(e => e.textContent)"
)
JS_PARTIAL_TEXTS = (
    "Array.from(document.querySelectorAll("
    "'li.transcript-segment--partial .transcript-segment__text'))"
    ".map(e => e.textContent)"
)
JS_PARTIAL_SPEAKERS = (
    "Array.from(document.querySelectorAll("
    "'li.transcript-segment--partial .transcript-segment__speaker'))"
    ".map(e => e.textContent)"
)
JS_BODY_TEXT = "document.body.innerText"


async def main() -> int:
    page = Page()
    await page.connect()

    # ---- create + start via the real UI-visible lifecycle (API is fine here;
    #      the lifecycle itself is covered elsewhere) ------------------------
    code, meeting = _http("POST", "/api/meetings", {"title": "ASR-0A 浏览器 E2E"})
    assert code == 201, meeting
    mid = meeting["id"]
    code, meeting = _http("POST", f"/api/meetings/{mid}/start")
    assert code == 200 and meeting["status"] == "IN_PROGRESS", meeting
    print(f"[e2e] meeting={mid} asrSessionId={SESSION}")

    # ---- open the meeting room in the REAL browser ------------------------
    await page.goto(f"{APP}/meetings/{mid}")
    ok = await page.wait_for(
        "document.body.innerText.includes('实时记录') || "
        "document.body.innerText.includes('结束会议')",
        timeout=25, label="room loaded",
    )
    _record("A15 browser opened the meeting room (SSE chain live)", ok)

    # ---- sentence 1: three PARTIAL revisions, speaker unknown -------------
    revs = [
        (1000, 1400, "我们今天"),
        (1000, 2600, "我们今天讨论会议助手"),
        (1000, 3200, "我们今天讨论会议助手的整体方案"),
    ]
    seen_counts: list[int] = []
    seen_texts: list[list[str]] = []
    for start_ms, end_ms, text in revs:
        c, _ = _http("POST", f"/api/internal/meetings/{mid}/asr-events",
                     _asr(mid, 1, kind="partial", start_ms=start_ms, end_ms=end_ms, text=text))
        assert c == 200, f"partial rejected {c}"
        await asyncio.sleep(1.2)
        seen_counts.append(await page.eval(JS_PARTIAL_COUNT))
        seen_texts.append(await page.eval(JS_PARTIAL_TEXTS) or [])

    _record("ASR-A04 consecutive PARTIALs render as ONE live line (in-place)",
            seen_counts == [1, 1, 1], f"partial_counts={seen_counts}")
    _record("ASR-A16 PARTIAL text updates in place, never stacks",
            [t[0] for t in seen_texts if t] == [r[2] for r in revs],
            f"texts={[t[0] if t else None for t in seen_texts]}")

    speakers = await page.eval(JS_PARTIAL_SPEAKERS)
    _record("ASR-A05 speakerId=-1 never renders as 'speaker_-1'",
            speakers == ["unknown"], f"speakers={speakers}")

    # ---- FINAL: the live line becomes a permanent line --------------------
    c, _ = _http("POST", f"/api/internal/meetings/{mid}/asr-events",
                 _asr(mid, 1, kind="final", speaker_id=0, start_ms=1000, end_ms=3200,
                      text="我们今天讨论会议助手的整体方案。"))
    assert c == 200, c
    await page.wait_for(f"{JS_PARTIAL_COUNT} === 0", timeout=15)
    finals1 = await page.eval(JS_FINAL_TEXTS) or []
    _record("ASR-A07 FINAL replaces the PARTIAL live line with ONE FINAL line",
            len(finals1) == 1 and finals1[0] == "我们今天讨论会议助手的整体方案。",
            f"final_lines={finals1}")

    seg = _http("GET", f"/api/meetings/{mid}/transcript")[1]["segments"]
    _record("ASR-A08 FINAL persisted with Transcript-owned segment_seq=1",
            len(seg) == 1 and seg[0]["segment_seq"] == 1 and seg[0]["speaker_id"] == "speaker_0",
            f"segs={[(s['segment_seq'], s['speaker_id']) for s in seg]}")

    # ---- sentence 2 (live, no refresh) ------------------------------------
    c, _ = _http("POST", f"/api/internal/meetings/{mid}/asr-events",
                 _asr(mid, 2, kind="partial", speaker_id=-1, start_ms=4000, end_ms=4600,
                      text="第一个版本"))
    assert c == 200
    await asyncio.sleep(1.2)
    c, _ = _http("POST", f"/api/internal/meetings/{mid}/asr-events",
                 _asr(mid, 2, kind="final", speaker_id=1, start_ms=4000, end_ms=5600,
                      text="第一个版本先把实时字幕跑通。"))
    assert c == 200
    await page.wait_for(f"{JS_FINAL_COUNT} === 2", timeout=15)
    finals2 = await page.eval(JS_FINAL_TEXTS) or []
    _record("ASR-A03/A11 two sentences -> exactly two FINAL lines, correct order",
            finals2 == ["我们今天讨论会议助手的整体方案。", "第一个版本先把实时字幕跑通。"],
            f"finals={finals2}")

    # ---- A10/A11: refresh -> Snapshot restore -> still streaming ----------
    await page.goto(f"{APP}/meetings/{mid}")
    ok = await page.wait_for(f"{JS_FINAL_COUNT} === 2", timeout=25)
    _record("ASR-A10 refresh restores both FINAL lines from Snapshot", ok,
            f"after_refresh={await page.eval(JS_FINAL_TEXTS)}")

    c, _ = _http("POST", f"/api/internal/meetings/{mid}/asr-events",
                 _asr(mid, 3, kind="final", speaker_id=0, start_ms=6000, end_ms=7000,
                      text="上游语音识别由其他同学负责。"))
    assert c == 200
    got3 = await page.wait_for(f"{JS_FINAL_COUNT} === 3", timeout=20)
    _record("ASR-A11 after refresh the reconnected stream still receives live FINAL",
            got3, f"final_count={await page.eval(JS_FINAL_COUNT)}")

    # ---- finish -> internal mock STREAM_END -> freeze -> summary ----------
    segs = _http("GET", f"/api/meetings/{mid}/transcript")[1]["segments"]
    last_seq = segs[-1]["segment_seq"]
    _record("ASR-A08b three persisted FINAL, seqs=[1,2,3]",
            [s["segment_seq"] for s in segs] == [1, 2, 3],
            f"seqs={[s['segment_seq'] for s in segs]}")

    c, _ = _http("POST", f"/api/meetings/{mid}/finish")
    assert c == 200, c
    c, body = _http("POST", f"/api/internal/meetings/{mid}/stream-end", {
        "event_id": f"evt_se_{int(time.time()*1000)}",
        "meeting_id": mid,
        "event_type": "TRANSCRIPT_STREAM_END",
        "last_segment_seq": last_seq,
    })
    _record("ASR-A17 internal mock STREAM_END froze the transcript",
            c == 200 and body.get("transcript_complete") is True, f"{body}")

    # ---- Result page: real persisted summary, driven by summary SSE -------
    await page.goto(f"{APP}/meetings/{mid}/result")
    ok = await page.wait_for(
        "document.body.innerText.includes('会议已完成') || "
        "document.body.innerText.includes('会议纪要')",
        timeout=35,
    )
    _record("ASR-A19 Result page opened after completion", ok)

    ok = await page.wait_for(
        "document.body.innerText.includes('会议讨论了小水华会议助手 MVP 方案')",
        timeout=35,
    )
    _record("ASR-A18 real persisted FakeProvider summary rendered in the browser",
            ok, f"text={((await page.eval(JS_BODY_TEXT)) or '')[:60]!r}")

    # ---- regenerate -> append-only v2, meeting stays COMPLETED ------------
    c, regen = _http("POST", f"/api/meetings/{mid}/summary/regenerate")
    v = regen.get("version")
    c2, meeting_after = _http("GET", f"/api/meetings/{mid}")
    _record("ASR-A20 regenerate allocates v2 and meeting stays COMPLETED",
            c == 202 and v == 2 and meeting_after.get("status") == "COMPLETED",
            f"http={c} version={v} meeting={meeting_after.get('status')}")

    await page.close()

    print(f"\nmeeting_id       = {mid}")
    print(f"asr_session_id   = {SESSION}")
    print(f"sentence ids     = 1, 2, 3")
    print(f"final count      = 3")
    print(f"segment seq      = [1, 2, 3]  (Transcript-owned, NOT sentenceId)")
    print(f"summary          = COMPLETED (v1) -> regenerated v2")

    print(f"\nTOTAL: {_PASS} PASS / {_FAIL} FAIL")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
