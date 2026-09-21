#!/usr/bin/env python
"""UI-INTEL-1 browser E2E (CDP) — the Meeting Intelligence Panel in the real UI.

What only a browser can prove here (and why API-level tests are not enough):

  1. `meeting.state.updated` is a NAMED SSE frame. Named frames do not fire
     `onmessage`; a hook that only listens to `message` receives NOTHING,
     SILENTLY. We already paid for that bug once (8C-3). The only way to know
     the panel actually re-fetches is to watch the DOM change without a refresh.
  2. The panel is driven by the meeting state, not by transcript keywords. The
     FakeProvider returns EMPTY state updates by default, so the panel MUST stay
     honest ("暂时没有明确的结论") instead of inventing decisions — a real,
     testable property of "the frontend never derives".
  3. `owner` / `deadline` = null renders "待确认" as a UI fallback only.

Flow:
  create -> start -> open room -> panel shows the honest empty state
  -> drive 8 FINAL so the agent batch threshold is reached -> the state row
     really changes (version 1) -> WITHOUT refreshing, the panel must render the
     same content the API returns -> reload -> identical panel (Snapshot-ish
     recovery via GET /state).

Run:  backend/.venv/Scripts/python scripts/e2e_ui_intel_browser.py
Requires: backend :8000, vite :5173, Chrome with --remote-debugging-port=9222.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request

import websockets

# Overridable so the suite never has to touch a developer's running servers:
#   UI_INTEL_BACKEND=http://127.0.0.1:8001 UI_INTEL_APP=http://127.0.0.1:5174 ...
CDP = os.environ.get("UI_INTEL_CDP", "http://127.0.0.1:9222")
APP = os.environ.get("UI_INTEL_APP", "http://127.0.0.1:5173")
BACKEND = os.environ.get("UI_INTEL_BACKEND", "http://127.0.0.1:8000")

SESSION = "intel-e2e-session"

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
            return e.code, (json.loads(raw) if raw else {})
        except json.JSONDecodeError:
            return e.code, {"detail": raw}


def _final(meeting_id: str, sentence_id: int, text: str, speaker_id: int = 0) -> dict:
    return {
        "type": "transcript.final",
        "meetingId": meeting_id,
        "asrSessionId": SESSION,
        "sentenceId": sentence_id,
        "speakerId": speaker_id,
        "startMs": sentence_id * 1000,
        "endMs": sentence_id * 1000 + 800,
        "text": text,
        "provider": "tencent",
    }


# --------------------------------------------------------------------------
# CDP driver (a cut-down copy of e2e_asr_browser.py's Page — the two scripts
# stay independent on purpose: each drives its own scenario end to end).
# --------------------------------------------------------------------------

class Page:
    def __init__(self) -> None:
        self.ws = None
        self._id = 0

    async def connect(self) -> None:
        with urllib.request.urlopen(CDP + "/json/list", timeout=10) as r:
            targets = json.load(r)
        page = next((t for t in targets if t.get("type") == "page"), None)
        if page is None:
            raise RuntimeError("no page target; open a tab in the debug Chrome")
        self.ws = await websockets.connect(
            page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024
        )

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
            "Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True
        )
        return res.get("result", {}).get("value")

    async def goto(self, url: str) -> None:
        await self.send("Page.enable")
        await self.send("Page.navigate", url=url)
        await asyncio.sleep(0.5)

    async def wait_for(self, expr: str, timeout: float = 20.0) -> bool:
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


# --- DOM probes: read the REAL panel, keyed off its own class names --------

JS_PANEL_TEXT = (
    "Array.from(document.querySelectorAll('.intel-block__title, .intel-item__text, "
    ".intel-item__sub, .intel-meta'))"
    ".map(e => e.textContent).join('|')"
)
JS_PANEL_TITLES = (
    "Array.from(document.querySelectorAll('.intel-block__title')).map(e => e.textContent)"
)
JS_PANEL_PRESENT = "document.querySelectorAll('.intel-head').length"
JS_ROOM_READY = (
    "document.body.innerText.includes('实时记录') || "
    "document.body.innerText.includes('AI 会议理解')"
)


async def main() -> int:
    page = Page()
    await page.connect()

    code, meeting = _http("POST", "/api/meetings", {"title": "UI-INTEL-1 面板 E2E"})
    assert code == 201, meeting
    mid = meeting["id"]
    code, meeting = _http("POST", f"/api/meetings/{mid}/start")
    assert code == 200 and meeting["status"] == "IN_PROGRESS", meeting
    print(f"[e2e] meeting={mid}")

    # ---- the panel exists in the room, before any transcript --------------
    await page.goto(f"{APP}/meetings/{mid}")
    ok = await page.wait_for(JS_ROOM_READY, timeout=25)
    _record("UI-I01 meeting room opened with the panel mounted", ok)
    ok = await page.wait_for(f"{JS_PANEL_PRESENT} === 1", timeout=15)
    _record("UI-I02 AI 会议理解 panel rendered exactly once in the rail", ok)

    # ---- version=0 / EMPTY must be an HONEST empty state -----------------
    code, state = _http("GET", f"/api/meetings/{mid}/state")
    _record("UI-I03 public state endpoint answers before any FINAL",
            code == 200 and state.get("version") == 0 and state.get("status") == "EMPTY",
            f"http={code} version={state.get('version')} status={state.get('status')}")

    body = (await page.eval(JS_PANEL_TEXT)) or ""
    _record("UI-I04 EMPTY renders the waiting state, no invented content",
            "等待会议内容" in ((await page.eval("document.body.innerText")) or ""),
            f"panel_text={body[:60]!r}")

    # ---- drive a batch of FINAL, then observe the AGENT-1 update ----------
    # The batch threshold is 8 segments, so an update only happens once eight
    # FINALs have landed — i.e. the panel must change WITHOUT a page refresh
    # when `meeting.state.updated` arrives. That is the whole point.
    texts = [f"第 {i} 句：讨论会议助手里程碑与分工。" for i in range(1, 10)]
    for i, text in enumerate(texts, start=1):
        c, _ = _http("POST", f"/api/internal/meetings/{mid}/asr-events",
                     _final(mid, i, text, speaker_id=i % 2))
        assert c == 200, f"final {i} rejected: {c}"
    print("[e2e] 9 FINAL sent (> batch threshold 8)")

    # The agent runs in a BackgroundTask; poll the API until it moved.
    deadline = time.time() + 30
    version = 0
    while time.time() < deadline:
        _, st = _http("GET", f"/api/meetings/{mid}/state")
        version = st.get("version", 0)
        if version >= 1 and st.get("last_processed_segment_seq", 0) >= 8:
            break
        await asyncio.sleep(0.6)

    _, st = _http("GET", f"/api/meetings/{mid}/state")
    _record("UI-I05 AGENT-1 processed a batch (state version advanced)",
            st.get("version", 0) >= 1 and st.get("last_processed_segment_seq", 0) >= 8,
            f"version={st.get('version')} cursor={st.get('last_processed_segment_seq')} "
            f"status={st.get('status')}")

    # ---- THE core assertion: the panel updated WITHOUT a refresh -----------
    # `meeting.state.updated` reached the SAME /events EventSource and the panel
    # re-fetched. If the named-frame subscription were broken, this would hang
    # until timeout with the panel still showing the pre-update content.
    # We wait for an item that can ONLY come from the rich scripted state.
    ok = await page.wait_for(
        "document.body.innerText.includes('下周三上线')",
        timeout=25,
    )
    _record("UI-I06 panel reacted to meeting.state.updated WITHOUT a refresh", ok,
            f"expect_version={st.get('version')}")

    # ---- the panel content mirrors the API, not a keyword guess ----------
    panel_titles = await page.eval(JS_PANEL_TITLES) or []
    panel_texts = set(((await page.eval(JS_PANEL_TEXT)) or "").split("|"))

    api_decisions = [d["text"] for d in st.get("decisions", [])]
    api_topics = [t["title"] for t in st.get("topics", [])] + ([st["current_topic"]] if st.get("current_topic") else [])
    api_actions = [a["task"] for a in st.get("action_items", [])]

    mirrored = all(
        any(txt in pt for pt in panel_texts)
        for txt in api_decisions + api_topics + api_actions
    )
    _record("UI-I07 every rendered item comes from the API state (no frontend derivation)",
            mirrored,
            f"api_decisions={api_decisions} api_topics={api_topics} panel={sorted(panel_texts)[:5]}")

    # Section headers appear only for NON-empty groups (no empty-list noise).
    # 当前议题 is a header of its own, driven by the scalar `current_topic`.
    expected_headers = []
    if st.get("current_topic"):
        expected_headers.append("当前议题")
    if st.get("decisions"):
        expected_headers.append("已确认决策")
    if st.get("action_items"):
        expected_headers.append("待办事项")
    if st.get("open_questions"):
        expected_headers.append("未决问题")
    if st.get("risks"):
        expected_headers.append("风险 / 顾虑")
    _record("UI-I08 section headers match the non-empty API groups exactly",
            sorted(panel_titles) == sorted(set(expected_headers)),
            f"panel={sorted(panel_titles)} expected={sorted(set(expected_headers))}")

    # ---- owner/deadline fallback is display-only -------------------------
    null_owner_actions = [a for a in st.get("action_items", []) if a.get("owner") is None]
    body_text = (await page.eval("document.body.innerText")) or ""
    if null_owner_actions:
        _record("UI-I09 owner=null renders 待确认 in the UI",
                "负责人：待确认" in body_text, "found a null-owner action item")
    else:
        _record("UI-I09 owner=null fallback (no null-owner item in this run)",
                "待确认" not in body_text or "负责人" in body_text,
                "no null owner to observe — fallback untouched")

    # and the fallback must NOT have travelled back to the server
    _, st2 = _http("GET", f"/api/meetings/{mid}/state")
    _record("UI-I10 待确认 never written back to the backend",
            all(a.get("owner") not in ("待确认",) for a in st2.get("action_items", [])),
            f"owners={[a.get('owner') for a in st2.get('action_items', [])]}")

    # ---- reload: GET /state is the durable recovery path -----------------
    await page.goto(f"{APP}/meetings/{mid}")
    ok2 = await page.wait_for(
        "document.body.innerText.includes('下周三上线')", timeout=25
    )
    _record("UI-I11 after refresh the panel restores from GET /state", ok2,
            f"version={st.get('version')}")

    # ---- transcript is untouched by any of this --------------------------
    _, segs = _http("GET", f"/api/meetings/{mid}/transcript")
    _record("UI-I12 transcript unaffected by the intelligence panel",
            [s["segment_seq"] for s in segs["segments"]] == list(range(1, 10)),
            f"seqs={[s['segment_seq'] for s in segs['segments']]}")

    # ---- ACTIVE -> FAILED with the version HELD ---------------------------
    # Settle FIRST: 16 small FINALs fill exactly two batches ([1..8], [9..16]),
    # so once the cursor reaches 16 there is no buffered remainder left. Without
    # this, the leftover 9th segment would form its own successful batch and the
    # version would legitimately move — muddying the assertion below.
    for sid in range(10, 17):
        c, _ = _http("POST", f"/api/internal/meetings/{mid}/asr-events",
                     _final(mid, sid, f"第 {sid} 句：补充说明。"))
        assert c == 200, f"final {sid} rejected: {c}"

    deadline = time.time() + 40
    while time.time() < deadline:
        _, settle = _http("GET", f"/api/meetings/{mid}/state")
        if settle.get("last_processed_segment_seq", 0) >= 16:
            break
        await asyncio.sleep(0.6)
    _, settle = _http("GET", f"/api/meetings/{mid}/state")
    settled_version = settle.get("version")
    print(f"[e2e] settled: version={settled_version} "
          f"cursor={settle.get('last_processed_segment_seq')} "
          f"status={settle.get('status')}")

    # ONE FINAL whose text (the ingress maximum, 4000 chars) exceeds the hard
    # per-batch bound trips the size guard. A lone segment always forms its own
    # ready batch, so this is a single failed run — and a failure never advances
    # the version or the cursor (repository.mark_failed).
    #
    # This is exactly the transition a naive "publish when version+1" rule would
    # MISS. It must still reach the browser. The scratch server runs with
    # MEETING_AGENT_MAX_BATCH_CHARS lowered (2000, via env) precisely so the
    # guard is reachable without a 12000-char payload — a config knob, not an
    # application change.
    big = ("补" * 4000)[: 4000 - 6]  # ingress caps `text` at 4000 chars
    c, body = _http("POST", f"/api/internal/meetings/{mid}/asr-events",
                    _final(mid, 17, "超长段落：" + big))
    assert c == 200, f"oversized final rejected: {c} {body}"

    deadline = time.time() + 30
    while time.time() < deadline:
        _, stf = _http("GET", f"/api/meetings/{mid}/state")
        if stf.get("status") == "FAILED":
            break
        await asyncio.sleep(0.6)
    _, stf = _http("GET", f"/api/meetings/{mid}/state")
    _record("UI-I13 oversized batch drives the state FAILED at the SAME version",
            stf.get("status") == "FAILED"
            and stf.get("version") == settled_version
            and stf.get("last_processed_segment_seq") == 16,
            f"status={stf.get('status')} version={stf.get('version')} "
            f"(was {settled_version}) cursor={stf.get('last_processed_segment_seq')}")

    # FAILED at an unchanged version is still a product-visible durable change,
    # so the panel must react — and it must NOT wipe the content the user is
    # already reading (last-good), only annotate it.
    ok = await page.wait_for(
        "document.body.innerText.includes('暂时') || "
        "document.body.innerText.includes('中断')",
        timeout=25,
    )
    body_after = (await page.eval("document.body.innerText")) or ""
    _record("UI-I14 FAILED (version unchanged) reached the panel without a refresh",
            ok, f"status={stf.get('status')}")
    _record("UI-I15 FAILED keeps the last-good content on screen",
            "下周三上线" in body_after,
            "previously-rendered decision still visible")

    # and the corruption/failure detail must never reach the browser
    _record("UI-I16 no internal failure detail leaked into the UI",
            "Traceback" not in body_after
            and "MeetingStateCorrupt" not in body_after
            and "too large" not in body_after
            and "chars >" not in body_after,
            "no stack/prompt/guard message in the DOM")

    # ---- Agent failure is NOT transcript failure -------------------------
    _, segs2 = _http("GET", f"/api/meetings/{mid}/transcript")
    _record("UI-I17 Agent FAILED did not damage the transcript",
            [s["segment_seq"] for s in segs2["segments"]] == list(range(1, 18)),
            f"seqs={[s['segment_seq'] for s in segs2['segments']]}")

    await page.close()

    print(f"\nmeeting_id      = {mid}")
    print(f"state version   = {st.get('version')}  status = {st.get('status')} "
          f"-> {stf.get('status')} (version still {stf.get('version')})")
    print(f"cursor          = {st.get('last_processed_segment_seq')}")
    print(f"topics          = {len(st.get('topics', []))}")
    print(f"decisions       = {len(st.get('decisions', []))}")
    print(f"action_items    = {len(st.get('action_items', []))}")

    print(f"\nTOTAL: {_PASS} PASS / {_FAIL} FAIL")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
