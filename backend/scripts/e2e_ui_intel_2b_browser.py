#!/usr/bin/env python
"""UI-INTEL-2B browser E2E (CDP) — Final Summary → Transcript Evidence Trace.

What only a browser can prove here (API tests cannot):

  1. 「查看原文」 on the **Result page** switches the AntD Tab and lands on a
     real, highlighted transcript row. The tab switch is DOM state; the landing
     is a real scroll inside the transcript container.
  2. **The async-tab trap.** `setActiveTab` is async and AntD lazily mounts the
     inactive pane. Calling the navigation synchronously after the switch finds
     no container while `contentSettled` is already true, so the hook reports
     「原始记录暂不可定位」 — a FALSE failure that only a real click can expose.
     (This is why the page defers through `pendingTraceRef` + an effect.)
  3. **Negative control.** A legacy / fallback summary must render NO evidence
     entry at all — not a disabled dummy (§3/§9). The same page, same transcript,
     same components: only the backend row differs.
  4. §6 dangling evidence: partial (`[4,5,999]`) must behave exactly like `[4,5]`
     (999 silently ignored, NOT reported as unfindable); all-missing must degrade
     to the light notice without crashing and without leaking any internal detail.
  5. The 2B frontend code never touches `raw_model_output` to build evidence —
     asserted against the source, since that is where the prohibition lives.

The two meetings are driven through the REAL chain (Mock ASR → Transcript
Domain → AGENT-1 → FakeProvider(V2) → AGENT-2 → Summary row). Nothing is written
into the DB by hand: the evidence seqs in the DOM must have travelled
state_json → prompt → V2 JSON → `raw_model_output` → public projection → DOM.

Run:  backend/.venv/Scripts/python scripts/e2e_ui_intel_2b_browser.py
Requires: backend :8002 (scratch, scripts.e2e_ui_intel_2b_app), vite :5175,
          Chrome on :9223. See the module header of the 2A suite for the launcher.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import websockets

# Overridable so the suite never touches a developer's running servers.
CDP = os.environ.get("UI2B_CDP", "http://127.0.0.1:9223")
APP = os.environ.get("UI2B_APP", "http://127.0.0.1:5175")
BACKEND = os.environ.get("UI2B_BACKEND", "http://127.0.0.1:8002")

SESSION = "ui2b-e2e-session"

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
        with urllib.request.urlopen(req, timeout=20) as r:
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
# CDP driver (same shape as the 2A suite — no shared module: these are
# standalone dev scripts, and a shared helper would be one more thing the
# regression suite depends on)
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

    async def wait_for(self, expr: str, timeout: float = 25.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if await self.eval(expr):
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.3)
        return False

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()


# --------------------------------------------------------------------------
# DOM probes — keyed off the REAL contract (`data-segment-seq`, `.intel-trace`,
# the tab pane), never off rendered prose, so a text tweak cannot make them pass.
# --------------------------------------------------------------------------

JS_ACTIVE_TAB = (
    "(() => { const t = document.querySelector('.ant-tabs-tab-active');"
    " return t ? (t.textContent || '').trim() : '' })()"
)

JS_TAB_LABELS = (
    "Array.from(document.querySelectorAll('.ant-tabs-tab'))"
    ".map(t => (t.textContent || '').trim())"
)

JS_CLICK_TAB = """((label) => {
  const tabs = Array.from(document.querySelectorAll('.ant-tabs-tab'));
  const t = tabs.find(x => (x.textContent || '').includes(label));
  if (!t) return 'no-tab';
  const btn = t.querySelector('.ant-tabs-tab-btn') || t;
  btn.click();
  return 'clicked';
})"""

# Count the evidence entries INSIDE the currently visible pane. A stale entry in
# the hidden pane must not count (that is why the active pane is scoped).
JS_TRACE_COUNT = """(() => {
  const pane = document.querySelector('.ant-tabs-tabpane-active');
  return pane ? pane.querySelectorAll('button.intel-trace').length : -1;
})()"""

JS_TRACE_LABELS = """(() => {
  const pane = document.querySelector('.ant-tabs-tabpane-active');
  if (!pane) return [];
  return Array.from(pane.querySelectorAll('button.intel-trace')).map(b => (b.textContent || '').trim());
})()"""

JS_CLICK_TRACE_IN_SUMMARY = """((needle) => {
  const wrap = document.querySelector('.summary-wrap');
  if (!wrap) return 'no-summary';
  const items = Array.from(wrap.querySelectorAll('li.summary-list__item'));
  for (const it of items) {
    const span = it.querySelector('span');
    if (!span || !(span.textContent || '').includes(needle)) continue;
    const b = it.querySelector('button.intel-trace');
    if (b) { b.click(); return 'clicked'; }
    return 'no-button';
  }
  return 'no-item';
})"""

JS_HAS_TRACE_FOR = """((needle) => {
  const wrap = document.querySelector('.summary-wrap');
  if (!wrap) return null;
  const items = Array.from(wrap.querySelectorAll('li.summary-list__item'));
  for (const it of items) {
    const span = it.querySelector('span');
    if (!span || !(span.textContent || '').includes(needle)) continue;
    return it.querySelector('button.intel-trace') !== null;
  }
  return null;
})"""

# Read the summary list items' TEXT. Deliberately NOT scoped to
# `li.summary-list__item > span`: the legacy/flat path renders plain `<li>` with
# no inner span (that markup exists only on the structured path), so a
# span-scoped probe returns [] for a perfectly well-rendered legacy summary and
# the negative-control assertions would fail for the wrong reason. Here the
# button is stripped out instead, so both paths are read the same way.
JS_SUMMARY_TEXTS = """(() => {
  const wrap = document.querySelector('.summary');
  if (!wrap) return [];
  return Array.from(wrap.querySelectorAll('.summary-list li')).map(li => {
    const clone = li.cloneNode(true);
    clone.querySelectorAll('button').forEach(b => b.remove());
    return (clone.textContent || '').trim();
  });
})()"""

JS_HIGHLIGHTED = (
    "Array.from(document.querySelectorAll('.transcript-segment--evidence'))"
    ".map(e => Number(e.getAttribute('data-segment-seq'))).sort((a,b) => a-b)"
)

JS_SCROLLBACK = (
    "(() => { const ol = document.querySelector('.transcript-segments');"
    " return ol ? Math.round(ol.scrollTop) : -1 })()"
)

JS_SCROLL_METRICS = """(() => {
  const ol = document.querySelector('.transcript-segments');
  if (!ol) return null;
  return { scrollTop: Math.round(ol.scrollTop), scrollHeight: ol.scrollHeight,
           clientHeight: ol.clientHeight, maxScroll: ol.scrollHeight - ol.clientHeight };
})()"""

JS_SEQ_IN_VIEW = """((seq) => {
  const ol = document.querySelector('.transcript-segments');
  const li = ol && ol.querySelector('[data-segment-seq="' + seq + '"]');
  if (!ol || !li) return false;
  const o = ol.getBoundingClientRect(), l = li.getBoundingClientRect();
  return l.top >= o.top - 2 && l.bottom <= o.bottom + 2;
})"""

JS_SEQ_COUNT = "document.querySelectorAll('.transcript-segments [data-segment-seq]').length"

JS_NOTE = (
    "(() => { const n = document.querySelector('.transcript-note');"
    " return n ? (n.textContent || '').trim() : '' })()"
)

JS_BODY = "document.body.innerText || ''"


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------
# 20 sentences: enough that the 460px transcript viewport genuinely overflows,
# so the evidence jump is observable at all. It also lands the AGENT-1 batch
# boundary at 16, which is what releases the state update before `finish`.
SEED_SENTENCES = 20
BATCH = 8


def _send_finals(mid: str, start: int, count: int) -> None:
    for sid in range(start, start + count):
        c, _ = _http(
            "POST",
            f"/api/internal/meetings/{mid}/asr-events",
            _final(mid, sid, f"第 {sid} 句：讨论会议助手里程碑与分工安排。", speaker_id=sid % 2),
        )
        assert c == 200, f"final {sid} rejected: {c}"


def _make_meeting(title: str) -> str:
    code, meeting = _http("POST", "/api/meetings", {"title": title})
    assert code == 201, meeting
    mid = meeting["id"]
    code, meeting = _http("POST", f"/api/meetings/{mid}/start")
    assert code == 200 and meeting["status"] == "IN_PROGRESS", meeting
    return mid


def _finish_and_stream_end(mid: str) -> None:
    """finish → internal mock STREAM_END → freeze → Finalizer → summary row."""
    segs = _http("GET", f"/api/meetings/{mid}/transcript")[1]["segments"]
    last_seq = segs[-1]["segment_seq"] if segs else 0
    c, _ = _http("POST", f"/api/meetings/{mid}/finish")
    assert c == 200, f"finish rejected: {c}"
    c, body = _http(
        "POST",
        f"/api/internal/meetings/{mid}/stream-end",
        {
            "event_id": f"evt_se_{mid}_{int(time.time() * 1000)}",
            "meeting_id": mid,
            "event_type": "TRANSCRIPT_STREAM_END",
            "last_segment_seq": last_seq,
        },
    )
    assert c == 200 and body.get("transcript_complete") is True, body


def _wait_summary(mid: str, *, timeout: float = 45) -> dict:
    deadline = time.time() + timeout
    body: dict = {}
    while time.time() < deadline:
        _, body = _http("GET", f"/api/meetings/{mid}/summary")
        if body.get("status") in ("COMPLETED", "FAILED"):
            return body
        time.sleep(0.5)
    return body


def _wait_meeting_status(mid: str, status: str, *, timeout: float = 30) -> str:
    deadline = time.time() + timeout
    cur = ""
    while time.time() < deadline:
        cur = _http("GET", f"/api/meetings/{mid}")[1].get("status", "")
        if cur == status:
            return cur
        time.sleep(0.4)
    return cur


# --------------------------------------------------------------------------
# Case 6: the source-level prohibition (checked in the repo, not the browser)
# --------------------------------------------------------------------------

FRONTEND_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"


def check_no_raw_model_output_reads() -> tuple[bool, str]:
    """Assert no 2B frontend module reads / parses `raw_model_output`.

    The type declaration keeps the field (the API still returns it, §4), and
    `MeetingResultPage` is allowed to mention it in prose. What is forbidden is
    an actual *read*: `summary.raw_model_output`, `JSON.parse(<that>)`, or a
    destructure of it.
    """
    # A read is a property access on something, i.e. `<expr>.raw_model_output`.
    # Bare `raw_model_output` in a comment / docstring / type declaration is not.
    read_re = re.compile(r"[\w\])}]\.raw_model_output\b")
    offenders: list[str] = []
    for path in sorted(FRONTEND_SRC.rglob("*")):
        if path.suffix not in (".ts", ".tsx"):
            continue
        text = path.read_text(encoding="utf-8")
        for m in read_re.finditer(text):
            line_no = text[: m.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(FRONTEND_SRC)}:{line_no}")
    return (not offenders), ", ".join(offenders)


# --------------------------------------------------------------------------

async def main() -> int:
    page = Page()
    await page.connect()

    # The scratch backend outlives a single run, so its FakeProvider shape is
    # NOT assumed — it is selected here. Phase A needs the rich V2 fixture.
    c, preset = _http("POST", "/_dev/fake-preset", {"preset": "v2"})
    assert c == 200 and preset.get("ok") is True, f"preset v2: {c} {preset}"

    # ======================================================================
    # Meeting A — the happy path. Driven through the REAL chain; the V2 JSON
    # comes from scripts/e2e_ui_intel_2b_app.py patching the FakeProvider's
    # defaults. Nothing is written to the DB by hand.
    # ======================================================================
    mid = _make_meeting("UI-INTEL-2B 会后证据溯源 E2E")
    print(f"[e2e] meeting A = {mid}")
    _send_finals(mid, 1, SEED_SENTENCES)
    _finish_and_stream_end(mid)
    summary = _wait_summary(mid)
    _record(
        "2B-01 a real V2 summary was produced end-to-end (meeting COMPLETED)",
        summary.get("status") == "COMPLETED"
        and _wait_meeting_status(mid, "COMPLETED") == "COMPLETED",
        f"summary.status={summary.get('status')} v={summary.get('version')}",
    )
    ev = summary.get("summary_evidence")
    _record(
        "2B-02 the public response carries the structured evidence projection",
        isinstance(ev, dict)
        and len(ev.get("decisions", [])) == 1
        and len(ev.get("action_items", [])) == 2
        and len(ev.get("risks", [])) == 1
        and len(ev.get("open_questions", [])) == 1,
        f"counts={ {k: len(v) for k, v in ev.items()} if ev else ev }",
    )
    _record(
        "2B-03 evidence seqs reached the API verbatim (999 NOT filtered at read time)",
        ev is not None
        and ev["action_items"][0]["source_segment_seqs"] == [4, 5, 999]
        and ev["risks"][0]["source_segment_seqs"] == [901, 902],
        f"action0={ev['action_items'][0]['source_segment_seqs'] if ev else None} "
        f"risk0={ev['risks'][0]['source_segment_seqs'] if ev else None}",
    )
    # Null owner / deadline must survive as null — the UI's 「待确认」 is a
    # presentation fallback, so a substituted value here would be a silent lie.
    _record(
        "2B-04 null owner / deadline survive the projection as null",
        ev is not None and ev["action_items"][1]["owner"] is None
        and ev["action_items"][1]["deadline"] is None,
        f"action1={ev['action_items'][1] if ev else None}",
    )

    # ---- open the Result page -------------------------------------------
    await page.goto(f"{APP}/meetings/{mid}/result")
    ok = await page.wait_for("document.querySelector('.result-tabs') !== null", timeout=25)
    _record("2B-05 Result page rendered", ok)
    ok = await page.wait_for(
        f"({JS_ACTIVE_TAB}).includes('会议纪要')", timeout=25
    )
    _record("2B-06 the Result page defaults to the 会议纪要 tab", ok,
            f"active={await page.eval(JS_ACTIVE_TAB)!r}")
    # §6: 会议纪要 / 会议内容 already exist — no route query was added.
    labels = await page.eval(JS_TAB_LABELS)
    _record("2B-07 the two existing tabs are the navigation surface (no new route/query)",
            labels == ["会议纪要", "会议内容"], f"labels={labels}")
    _record("2B-08 URL carries no trace query parameter",
            "trace" not in (await page.eval("window.location.search") or ""),
            f"search={await page.eval('window.location.search')!r}")

    # ---- Case 1: Decision → 查看原文 → tab switches → highlighted -------
    ok = await page.wait_for(f"{JS_TRACE_COUNT} > 0", timeout=15)
    _record("2B-09 「查看原文」 entries rendered on the summary items", ok,
            f"count={await page.eval(JS_TRACE_COUNT)}")
    _record("2B-10 the entry label is 「查看原文」 (§9, no seqs / no evidence ids shown)",
            (await page.eval(JS_TRACE_LABELS)) == ["查看原文"] * (await page.eval(JS_TRACE_COUNT)),
            f"labels={await page.eval(JS_TRACE_LABELS)}")
    texts = await page.eval(JS_SUMMARY_TEXTS) or []
    _record("2B-11 neither the raw seqs nor an internal id leak into the visible text",
            not any(re.search(r"\b(901|902|999)\b", t) for t in texts)
            and not any("raw_model_output" in t for t in texts),
            f"texts={texts}")

    # The tab switch is async; the pane must be mounted before the click lands.
    res = await page.eval(f"({JS_CLICK_TRACE_IN_SUMMARY})('下周三上线')")
    assert res == "clicked", f"decision trace button: {res}"
    ok = await page.wait_for(f"({JS_ACTIVE_TAB}).includes('会议内容')", timeout=8)
    _record("2B-12 Case 1: 「查看原文」 auto-switched to the 会议内容 tab", ok,
            f"active={await page.eval(JS_ACTIVE_TAB)!r}")
    await asyncio.sleep(1.6)  # let the smooth scroll settle
    hl = await page.eval(JS_HIGHLIGHTED)
    settled = await page.wait_for(f"{JS_SEQ_COUNT} === {SEED_SENTENCES}", timeout=15)
    _record("2B-13 ★ Case 1: the corresponding transcript rows are highlighted "
            "(and NOT a false 「原始记录暂不可定位」)",
            settled and hl == [1, 2] and await page.eval(JS_NOTE) == "",
            f"highlighted={hl} note={await page.eval(JS_NOTE)!r} seqs={await page.eval(JS_SEQ_COUNT)}")
    _record("2B-14 the evidence row was scrolled into view (min valid seq is the anchor)",
            await page.eval(f"({JS_SEQ_IN_VIEW})(1)") is True,
            f"scrollTop={await page.eval(JS_SCROLLBACK)}")

    # precondition for the next cases: the transcript must genuinely overflow
    metrics = await page.eval(JS_SCROLL_METRICS)
    _record("2B-15 the Result-page transcript is actually scrollable (jump is observable)",
            bool(metrics) and metrics["maxScroll"] > 100, f"metrics={metrics}")

    # ---- back to 会议纪要 ------------------------------------------------
    res = await page.eval(f"({JS_CLICK_TAB})('会议纪要')")
    assert res == "clicked", f"tab switch back: {res}"
    await page.wait_for(f"({JS_ACTIVE_TAB}).includes('会议纪要')", timeout=8)
    await page.wait_for("document.querySelectorAll('.transcript-segment--evidence').length === 0", timeout=8)

    # ---- Case 2: Action Item [4,5] → BOTH highlighted -------------------
    res = await page.eval(f"({JS_CLICK_TRACE_IN_SUMMARY})('后端接口联调')")
    assert res == "clicked", f"action trace button: {res}"
    await page.wait_for(f"({JS_ACTIVE_TAB}).includes('会议内容')", timeout=8)
    await asyncio.sleep(1.6)
    hl = await page.eval(JS_HIGHLIGHTED)
    _record("2B-16 Case 2: action item 「查看原文」 highlights BOTH seqs 4 and 5",
            hl == [4, 5], f"highlighted={hl}")
    _record("2B-17 the anchor is the MINIMUM valid seq (4)",
            await page.eval(f"({JS_SEQ_IN_VIEW})(4)") is True,
            f"seq4_in_view={await page.eval(f'({JS_SEQ_IN_VIEW})(4)')}")

    # ---- Case 3: partial dangling [4,5,999] must equal [4,5] ------------
    # The click above already carried 999 — it is asserted here explicitly,
    # because "the extra seq is silently dropped" is the whole point of §8.
    _record("2B-18 ★ Case 3: [4,5,999] behaves exactly like [4,5] — 999 is "
            "ignored and NOT reported as unfindable",
            hl == [4, 5] and await page.eval(JS_NOTE) == "",
            f"highlighted={hl} note={await page.eval(JS_NOTE)!r}")

    # ---- Case 4: all evidence absent → light notice, no crash -----------
    res = await page.eval(f"({JS_CLICK_TAB})('会议纪要')")
    assert res == "clicked", f"tab switch for case 4: {res}"
    await page.wait_for(f"({JS_ACTIVE_TAB}).includes('会议纪要')", timeout=8)
    await page.wait_for("document.querySelectorAll('.transcript-segment--evidence').length === 0", timeout=8)
    res = await page.eval(f"({JS_CLICK_TRACE_IN_SUMMARY})('支付接口稳定性需要确认')")
    assert res == "clicked", f"all-missing trace button: {res}"
    ok = await page.wait_for(
        "(() => { const n = document.querySelector('.transcript-note');"
        " return !!n && n.textContent.includes('暂不可定位') })()",
        timeout=8,
    )
    _record("2B-19 Case 4: all-evidence-absent degrades to 「原始记录暂不可定位」", ok,
            f"note={await page.eval(JS_NOTE)!r}")
    hl = await page.eval(JS_HIGHLIGHTED)
    _record("2B-20 Case 4: nothing was highlighted for the unfindable item",
            hl == [], f"highlighted={hl}")
    body = await page.eval(JS_BODY) or ""
    _record("2B-21 Case 4: the page did not crash and leaked no internal detail",
            "Traceback" not in body
            and "raw_model_output" not in body
            and await page.eval("document.querySelector('.summary') !== null") is True,
            "summary still rendered after the degradation")
    # The notice auto-expires; clicking another entry during the sticky window
    # would be an OVERWRITE assertion, not the recovery one. Wait it out so the
    # click below is unambiguously a fresh navigation.
    ok = await page.wait_for(f"{JS_NOTE} === ''", timeout=6)
    _record("2B-21b the 「暂不可定位」 notice auto-expires (not sticky, §6)", ok,
            f"note={await page.eval(JS_NOTE)!r}")
    # and navigating again must still work (the notice did not break anything)
    res = await page.eval(f"({JS_CLICK_TRACE_IN_SUMMARY})('下周三上线')")
    assert res == "clicked", f"recovery trace button: {res}"
    ok = await page.wait_for(f"{JS_HIGHLIGHTED}.length === 2", timeout=8)
    _record("2B-22 the evidence entry keeps working after a degradation", ok,
            f"highlighted={await page.eval(JS_HIGHLIGHTED)}")

    # ---- repeat click must re-trigger (§12, same as 2A) -----------------
    ok = await page.wait_for("document.querySelectorAll('.transcript-segment--evidence').length === 0", timeout=8)
    _record("2B-23 the highlight auto-expires (~2.4s)", ok)
    res = await page.eval(f"({JS_CLICK_TRACE_IN_SUMMARY})('下周三上线')")
    assert res == "clicked", f"repeat trace button: {res}"
    ok = await page.wait_for(f"{JS_HIGHLIGHTED}.length === 2", timeout=8)
    _record("2B-24 clicking the SAME evidence again re-triggers the highlight", ok)

    # ---- no auto-follow was dragged into the Result page (§7) -----------
    # The Result page must not offer the live-transcript FAB: there is no live
    # stream here, so a 「回到最新」 button would be a lie about the page.
    _record("2B-25 §7: the Result page exposes no 「回到最新」 auto-follow control",
            await page.eval("document.querySelector('.transcript-jump') === null") is True,
            "会后静态转写，不引入 auto-follow")

    # ---- refresh keeps working (no Zustand dependence) ------------------
    await page.goto(f"{APP}/meetings/{mid}/result")
    ok = await page.wait_for(f"{JS_TRACE_COUNT} > 0", timeout=25)
    _record("2B-26 after F5 the evidence entries come back from the durable API", ok,
            f"count={await page.eval(JS_TRACE_COUNT)}")

    # ======================================================================
    # Meeting B — NEGATIVE CONTROL (Case 5). Identical product code path; only
    # what the fake model "says" differs. An EMPTY state update makes
    # `assess_usability` return `state_empty`, which is §9's designed fallback
    # to the full-transcript legacy path. That prompt carries neither delimiter,
    # so the fake answers with the LEGACY shape: a real, populated summary with
    # `summary_evidence = null`. This is the honest control — the page has
    # content to render and must still offer NO 「查看原文」.
    #
    # (Plain "too few FINALs" does NOT work: `finalize_for_meeting` force-flushes
    # pending segments first, so a state row always exists and the V2 branch
    # always runs. See the harness route's docstring.)
    # ======================================================================
    c, preset = _http("POST", "/_dev/fake-preset", {"preset": "empty_state"})
    _record("2B-27 the negative-control preset was selected (harness knob)",
            c == 200 and preset.get("ok") is True, f"{c} {preset}")

    mid_b = _make_meeting("UI-INTEL-2B legacy 摘要（负向对照）")
    print(f"[e2e] meeting B = {mid_b}")
    _send_finals(mid_b, 1, 3)
    _finish_and_stream_end(mid_b)
    summary_b = _wait_summary(mid_b)
    _record("2B-28 Case 5 precondition: the fallback meeting produced a COMPLETED summary",
            summary_b.get("status") == "COMPLETED",
            f"status={summary_b.get('status')} v={summary_b.get('version')}")
    _record("2B-29 Case 5: legacy / fallback summary → summary_evidence is null",
            summary_b.get("summary_evidence", "MISSING") is None,
            f"evidence={summary_b.get('summary_evidence', 'MISSING')!r}")
    # …and it is still a fully usable summary (the legacy fields are untouched)
    _record("2B-30 §11: the pre-existing flat fields are unchanged by this step",
            bool(summary_b.get("overview")) and isinstance(summary_b.get("conclusions"), list)
            and "raw_model_output" in summary_b and "model_name" in summary_b,
            f"overview={summary_b.get('overview')!r} conclusions={summary_b.get('conclusions')}")

    await page.goto(f"{APP}/meetings/{mid_b}/result")
    ok = await page.wait_for(f"({JS_ACTIVE_TAB}).includes('会议纪要')", timeout=25)
    _record("2B-31 the fallback meeting's Result page rendered", ok)
    ok = await page.wait_for("document.querySelector('.summary') !== null", timeout=20)
    items = await page.eval(JS_SUMMARY_TEXTS) or []
    trace_count = await page.eval(JS_TRACE_COUNT)
    _record("2B-32 ★ Case 5: no structured evidence → NO 「查看原文」 entry at all "
            "(not a disabled dummy)",
            ok and trace_count == 0 and len(items) > 0,
            f"trace_buttons={trace_count} summary_items={items}")
    _record("2B-33 the fallback summary's content is still shown (nothing was lost)",
            any("MVP 只跑通两条主链路" in t for t in items), f"items={items}")
    # switching to 会议内容 must still work — the transcript itself is fine
    res = await page.eval(f"({JS_CLICK_TAB})('会议内容')")
    assert res == "clicked", f"transcript tab: {res}"
    ok = await page.wait_for(f"{JS_SEQ_COUNT} === 3", timeout=15)
    _record("2B-34 the transcript tab still renders for a legacy summary", ok,
            f"segments={await page.eval(JS_SEQ_COUNT)}")

    # ======================================================================
    # Case 6 — source level
    # ======================================================================
    ok, offenders = check_no_raw_model_output_reads()
    _record("2B-35 ★ Case 6: no frontend module reads / parses raw_model_output",
            ok, f"offenders={offenders or 'none'}")

    # ---- transcript integrity: no route query, no extra segments --------
    _, segs_a = _http("GET", f"/api/meetings/{mid}/transcript")
    _record("2B-36 the transcript is untouched by evidence navigation",
            [s["segment_seq"] for s in segs_a["segments"]] == list(range(1, SEED_SENTENCES + 1)),
            f"count={len(segs_a['segments'])} expected={SEED_SENTENCES}")

    await page.close()

    print(f"\nmeeting A (V2)      = {mid}")
    print(f"meeting B (fallback) = {mid_b}")
    print(f"\nTOTAL: {_PASS} PASS / {_FAIL} FAIL")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
