#!/usr/bin/env python
"""UI-INTEL-2A browser E2E (CDP) — Transcript Evidence Trace in the real UI.

What only a browser can prove here (and why API-level tests are not enough):

  1. 「查看原文」actually MOVES the transcript. Scrolling, highlighting and the
     `data-segment-seq` lookup are DOM facts — an API test can only assert that
     the seqs came back, never that the right line was found and revealed.
  2. THE core risk of this step (§10): after jumping to evidence, an incoming
     FINAL must NOT drag the user back to the bottom. That is a race between the
     SSE stream and the scroll container, and it only exists in a browser.
  3. The recovery path is the existing FAB — auto-follow must come back only
     when the user clicks it, and the transcript must then follow again.
  4. §12 repeat-click must re-trigger. A naive implementation (React state that
     doesn't change) silently no-ops on the second click; only a real click
     sequence catches that.
  5. §6 missing evidence degrades to a light notice — no dialog, no error, and
     the transcript keeps working.
  6. UI-INTEL-1 must still work: the panel still updates WITHOUT a refresh when
     `meeting.state.updated` arrives (the named-frame path).

Flow:
  create -> start -> open room -> panel renders with evidence entries
  -> click a DECISION「查看原文」-> seq 1 revealed + highlighted
  -> click an ACTION「查看依据」(seqs [4,5]) -> BOTH highlighted
  -> send a new FINAL -> the view must NOT jump back to the bottom
  -> click「回到最新」-> auto-follow restored (new FINAL scrolls again)
  -> click the dangling-evidence item ([99]) -> light notice, no dialog
  -> an item with no evidence renders NO entry at all
  -> UI-INTEL-1 regression: the panel reflects a state update without a refresh

Run:  backend/.venv/Scripts/python scripts/e2e_ui_intel_2a_browser.py
Requires: backend :8001 (scratch), vite :5175, Chrome on :9223.
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

# Overridable so the suite never has to touch a developer's running servers.
CDP = os.environ.get("UI2A_CDP", "http://127.0.0.1:9223")
APP = os.environ.get("UI2A_APP", "http://127.0.0.1:5175")
BACKEND = os.environ.get("UI2A_BACKEND", "http://127.0.0.1:8001")

SESSION = "intel-2a-e2e-session"

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
# CDP driver
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
            await asyncio.sleep(0.3)
        return False

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()


# --------------------------------------------------------------------------
# DOM probes — all keyed off the REAL contract (data-segment-seq), so the test
# cannot pass by matching rendered text.
# --------------------------------------------------------------------------

JS_SCROLLBACK = "(() => { const ol = document.querySelector('.transcript-segments'); return ol ? Math.round(ol.scrollTop) : -1 })()"

JS_SCROLL_METRICS = """(() => {
  const ol = document.querySelector('.transcript-segments');
  if (!ol) return null;
  return { scrollTop: Math.round(ol.scrollTop), scrollHeight: ol.scrollHeight,
           clientHeight: ol.clientHeight, maxScroll: ol.scrollHeight - ol.clientHeight };
})()"""

JS_HIGHLIGHTED = (
    "Array.from(document.querySelectorAll('.transcript-segment--evidence'))"
    ".map(e => Number(e.getAttribute('data-segment-seq'))).sort((a,b) => a-b)"
)

# Is a given seq visible INSIDE the transcript list's own viewport?
# Uses the container rect, not the window: the list scrolls, the page does not.
JS_SEQ_IN_VIEW = """((seq) => {
  const ol = document.querySelector('.transcript-segments');
  const li = ol && ol.querySelector('[data-segment-seq="' + seq + '"]');
  if (!ol || !li) return false;
  const o = ol.getBoundingClientRect(), l = li.getBoundingClientRect();
  return l.top >= o.top - 2 && l.bottom <= o.bottom + 2;
})"""

JS_VISIBLE_SEQS = """(() => {
  const ol = document.querySelector('.transcript-segments');
  if (!ol) return [];
  const o = ol.getBoundingClientRect();
  return Array.from(ol.querySelectorAll('[data-segment-seq]'))
    .filter(li => {
      const l = li.getBoundingClientRect();
      return l.top >= o.top - 2 && l.bottom <= o.bottom + 2;
    })
    .map(li => Number(li.getAttribute('data-segment-seq')));
})()"""

JS_SEQ_COUNT = "document.querySelectorAll('.transcript-segments [data-segment-seq]').length"

JS_CLICK_TRACE_BY_TEXT = """((needle) => {
  const items = Array.from(document.querySelectorAll('.intel-item'));
  for (const item of items) {
    const txt = item.textContent || '';
    if (!txt.includes(needle)) continue;
    const btn = item.querySelector('button.intel-trace');
    if (btn) { btn.click(); return 'clicked'; }
    return 'no-button';
  }
  return 'no-item';
})"""

JS_HAS_TRACE_FOR = """((needle) => {
  const items = Array.from(document.querySelectorAll('.intel-item'));
  for (const item of items) {
    const txt = item.textContent || '';
    if (!txt.includes(needle)) continue;
    return item.querySelector('button.intel-trace') !== null;
  }
  return null;
})"""

JS_NOTE = "(() => { const n = document.querySelector('.transcript-note'); return n ? n.textContent : '' })()"
JS_JUMP_VISIBLE = "document.querySelector('.transcript-jump') !== null"
JS_JUMP_TEXT = "(() => { const b = document.querySelector('.transcript-jump'); return b ? b.textContent : '' })()"

# ---- seeding -------------------------------------------------------------
# Enough sentences that the list genuinely overflows its 460px viewport, so the
# jump is observable at all (with ~10 rows the list is exactly as tall as its
# content and NOTHING scrolls — every scroll assertion would be vacuous).
SEED_SENTENCES = 20
# The agent's batch threshold is 8 segments, so batches land at 8 / 16 / 24 …
# Only SEGMENT count matters here: these sentences are ~21 chars each, far below
# MEETING_AGENT_BATCH_MAX_CHARS (1200).
BATCH = 8


def _send_finals(mid: str, start: int, count: int) -> None:
    for sid in range(start, start + count):
        c, _ = _http("POST", f"/api/internal/meetings/{mid}/asr-events",
                     _final(mid, sid, f"第 {sid} 句：讨论会议助手里程碑与分工安排。",
                            speaker_id=sid % 2))
        assert c == 200, f"final {sid} rejected: {c}"


def _wait_state(mid: str, *, version: int = 0, cursor: int = 0, timeout: float = 40) -> dict:
    deadline = time.time() + timeout
    state: dict = {}
    while time.time() < deadline:
        _, state = _http("GET", f"/api/meetings/{mid}/state")
        if state.get("version", 0) >= version and state.get("last_processed_segment_seq", 0) >= cursor:
            return state
        time.sleep(0.5)
    return state


async def main() -> int:
    page = Page()
    await page.connect()

    code, meeting = _http("POST", "/api/meetings", {"title": "UI-INTEL-2A 证据溯源 E2E"})
    assert code == 201, meeting
    mid = meeting["id"]
    code, meeting = _http("POST", f"/api/meetings/{mid}/start")
    assert code == 200 and meeting["status"] == "IN_PROGRESS", meeting
    print(f"[e2e] meeting={mid}")

    # ---- seed enough FINAL that the transcript genuinely overflows --------
    # Positions matter: seq 1 sits far ABOVE the fold (must be scrolled TO) and
    # seq 5 is near the bottom (roughly the initial position), so jumping to a
    # decision [1,2] and to an action item [4,5] pull in OPPOSITE directions.
    _send_finals(mid, 1, SEED_SENTENCES)

    # ---- agent batch -> state with evidence ------------------------------
    state = _wait_state(mid, version=1, cursor=BATCH)
    _record("2A-01 agent produced a state with item evidence",
            state.get("version", 0) >= 1 and state.get("last_processed_segment_seq", 0) >= BATCH
            and any(d.get("source_segment_seqs") for d in state.get("decisions", [])),
            f"version={state.get('version')} cursor={state.get('last_processed_segment_seq')}")
    print(f"[e2e] state version={state.get('version')} "
          f"cursor={state.get('last_processed_segment_seq')} "
          f"decisions={[d.get('source_segment_seqs') for d in state.get('decisions', [])]} "
          f"actions={[a.get('source_segment_seqs') for a in state.get('action_items', [])]}")

    await page.goto(f"{APP}/meetings/{mid}")
    ok = await page.wait_for("document.body.innerText.includes('AI 会议理解')", timeout=25)
    _record("2A-02 meeting room opened with the intelligence panel", ok)
    ok = await page.wait_for("document.body.innerText.includes('下周三上线')", timeout=25)
    _record("2A-03 panel rendered the state content", ok)
    ok = await page.wait_for("document.querySelectorAll('button.intel-trace').length > 0", timeout=15)
    _record("2A-04 「查看原文」entries rendered for items WITH evidence", ok)

    # §6: an item whose source_segment_seqs is [] must render NO entry at all
    no_evidence_has_button = await page.eval(
        f"({JS_HAS_TRACE_FOR})('监控告警阈值定多少？')"
    )
    _record("2A-05 item without evidence renders NO entry (not a disabled dummy)",
            no_evidence_has_button is False,
            f"has_button={no_evidence_has_button}")

    # ---- precondition: the list must genuinely scroll ---------------------
    # Without this, every scroll assertion below is vacuous (10 rows fit exactly).
    await asyncio.sleep(1.5)  # let the snapshot first-paint settle at the bottom
    metrics = await page.eval(JS_SCROLL_METRICS)
    _record("2A-06 transcript list is actually scrollable (jump is observable)",
            bool(metrics) and metrics["maxScroll"] > 100,
            f"metrics={metrics}")
    baseline_top = metrics["scrollTop"] if metrics else 0
    _record("2A-06b fresh page starts pinned to the bottom (auto-follow default)",
            baseline_top >= (metrics["maxScroll"] - 5 if metrics else 10**9),
            f"scrollTop={baseline_top} maxScroll={metrics['maxScroll'] if metrics else None}")

    # ---- decision -> seq 1 (must SCROLL UP) ------------------------------
    res = await page.eval(f"({JS_CLICK_TRACE_BY_TEXT})('下周三上线')")
    assert res == "clicked", f"decision trace button: {res}"
    await asyncio.sleep(1.6)  # let the scroll settle
    hl = await page.eval(JS_HIGHLIGHTED)
    in_view = await page.eval(f"({JS_SEQ_IN_VIEW})(1)")
    after_top = await page.eval(JS_SCROLLBACK)
    _record("2A-07 decision「查看原文」highlights its evidence seqs 1,2",
            hl == [1, 2], f"highlighted={hl}")
    _record("2A-08 decision「查看原文」scrolled seq 1 into view", in_view is True,
            f"seq1_in_view={in_view}")
    _record("2A-09 the view actually moved up from the bottom",
            after_top < baseline_top - 50, f"after={after_top} baseline={baseline_top}")

    # ---- repeat click must re-trigger (§12) ------------------------------
    # Wait for the first highlight to expire, then click the SAME entry: with a
    # state-based implementation the second click is a no-op and this fails.
    ok = await page.wait_for("document.querySelectorAll('.transcript-segment--evidence').length === 0", timeout=8)
    _record("2A-10 highlight auto-expires (~2.4s)", ok)
    res = await page.eval(f"({JS_CLICK_TRACE_BY_TEXT})('下周三上线')")
    assert res == "clicked", f"second decision click: {res}"
    ok = await page.wait_for("document.querySelectorAll('.transcript-segment--evidence').length > 0", timeout=5)
    _record("2A-11 clicking the SAME evidence again re-triggers the highlight", ok)

    # ---- action item -> seqs [4,5]: BOTH highlighted, scroll toward them --
    before_action = await page.eval(JS_SCROLLBACK)
    await page.eval(f"({JS_CLICK_TRACE_BY_TEXT})('后端接口联调')")
    await asyncio.sleep(1.6)
    hl = await page.eval(JS_HIGHLIGHTED)
    in_view4 = await page.eval(f"({JS_SEQ_IN_VIEW})(4)")
    after_action = await page.eval(JS_SCROLLBACK)
    _record("2A-12 action item「查看依据」highlights BOTH evidence seqs 4,5",
            hl == [4, 5], f"highlighted={hl}")
    _record("2A-13 anchor is the MINIMUM valid seq (4), and it IS revealed",
            in_view4 is True, f"seq4_in_view={in_view4}")

    # ---- …and the OPPOSITE direction: a decision near the END of the list --
    # [4,5] cannot prove "scroll down": with ~10 rows fitting the 460px viewport
    # seq 4 is already inside it at scrollTop=0, so centering legitimately clamps
    # to 0 and NOTHING moves — a "moved down" assertion here would be asserting
    # a bug. 灰度先全量 cites [18,20], far below the fold, so this one MUST move
    # the view down. (This is exactly the assertion 2A-13b got wrong.)
    before_down = await page.eval(JS_SCROLLBACK)
    res = await page.eval(f"({JS_CLICK_TRACE_BY_TEXT})('灰度先全量')")
    assert res == "clicked", f"late decision trace button: {res}"
    await asyncio.sleep(1.6)
    hl = await page.eval(JS_HIGHLIGHTED)
    after_down = await page.eval(JS_SCROLLBACK)
    _record("2A-13c「查看原文」on a late decision highlights BOTH seqs 18,20",
            hl == [18, 20], f"highlighted={hl}")
    _record("2A-13d jumping to [18,20] moved the view DOWN from [4,5]",
            after_down > before_down, f"after={after_down} before={before_down}")
    in_view18 = await page.eval(f"({JS_SEQ_IN_VIEW})(18)")
    in_view20 = await page.eval(f"({JS_SEQ_IN_VIEW})(20)")
    _record("2A-13e anchor is 18 (the minimum), and it IS revealed",
            in_view18 is True, f"seq18_in_view={in_view18}")
    _record("2A-13f both evidence rows are on screen together (18 and 20)",
            in_view20 is True,
            f"seq20_in_view={in_view20} (a 3-row span must fit the 460px viewport)")

    # Park back at the [1,2] anchor so the §10 assertion below starts from a
    # NON-bottom position — otherwise "the view did not move" passes vacuously.
    await page.eval(f"({JS_CLICK_TRACE_BY_TEXT})('下周三上线')")
    await asyncio.sleep(1.6)
    parked = await page.eval(JS_SCROLLBACK)
    _record("2A-13g re-parked away from the bottom (2A-15 precondition)",
            parked < (await page.eval(JS_SCROLL_METRICS))["maxScroll"] - 50,
            f"parked={parked}")

    # ---- §10 CORE: a new FINAL must NOT steal the scroll position --------
    # The user is parked at the evidence from the previous step. Send a FINAL:
    # if evidence navigation had NOT paused auto-follow, this would yank the
    # view to the bottom and the assertion below fails.
    before = await page.eval(JS_SCROLLBACK)
    c, _ = _http("POST", f"/api/internal/meetings/{mid}/asr-events",
                 _final(mid, SEED_SENTENCES + 1, "这条新转写不应该把用户拽回底部。", speaker_id=1))
    assert c == 200, f"extra final rejected: {c}"
    ok = await page.wait_for(
        f"{JS_SEQ_COUNT} === {SEED_SENTENCES + 1}", timeout=15
    )
    _record("2A-14 the new FINAL really arrived in the transcript", ok)
    await asyncio.sleep(1.2)
    after = await page.eval(JS_SCROLLBACK)
    _record("2A-15 ★ new FINAL did NOT drag the user back to the bottom",
            after == before, f"before={before} after={after}")

    # ---- the FAB is the only way back to auto-follow ---------------------
    _record("2A-16 「回到最新」FAB is offered after the jump",
            await page.eval(JS_JUMP_VISIBLE),
            f"text={(await page.eval(JS_JUMP_TEXT))!r}")
    await page.eval("document.querySelector('.transcript-jump').click()")
    await asyncio.sleep(1.6)
    bottom_now = await page.eval(JS_SCROLLBACK)
    max_scroll = (await page.eval(JS_SCROLL_METRICS))["maxScroll"]
    _record("2A-17 clicking「回到最新」scrolls back to the bottom",
            bottom_now >= max_scroll - 5, f"scrollTop={bottom_now} maxScroll={max_scroll}")

    # auto-follow must be genuinely restored: the NEXT final scrolls again.
    # This is the assertion that distinguishes "scrolled to the bottom once"
    # from "auto-follow is actually back on".
    c, _ = _http("POST", f"/api/internal/meetings/{mid}/asr-events",
                 _final(mid, SEED_SENTENCES + 2, "恢复跟随后这条应该自动滚到底部可见。", speaker_id=0))
    assert c == 200, f"extra final 2 rejected: {c}"
    ok = await page.wait_for(f"{JS_SEQ_COUNT} === {SEED_SENTENCES + 2}", timeout=15)
    await asyncio.sleep(1.2)
    after_resume = await page.eval(JS_SCROLLBACK)
    new_max = (await page.eval(JS_SCROLL_METRICS))["maxScroll"]
    _record("2A-18 auto-follow resumed: the next FINAL scrolls to the new bottom",
            ok and after_resume > bottom_now and after_resume >= new_max - 5,
            f"scrollTop={after_resume} (was {bottom_now}, new max {new_max})")

    # ---- §10 STRICT: the pause must work even when NOTHING scrolls --------
    # Everything above this line is order-dependent: useAutoScroll.onScroll
    # re-derives autoFollowRef from geometry, so an evidence jump that MOVES the
    # view ends up paused whether or not handleTraceEvidence() called
    # pauseAutoFollow() — a mutation that deletes the call still passed the
    # whole section above. The one case geometry cannot rescue is a jump that
    # scrolls NOTHING (target already on screen): no `scroll` event fires, so
    # onScroll never runs and only the explicit pause can suspend follow.
    #
    # [18,20] is that target right now — 2A-18 just parked us at the bottom and
    # seq 18 sits inside the final viewport.
    #
    # NOTE this deliberately documents a REAL weakness rather than hiding it:
    # for the ordinary case (a jump that scrolls) the explicit pause is largely
    # redundant with onScroll's geometric re-derivation. Keep calling it — it is
    # the only thing that holds in the no-scroll case, and it keeps intent
    # explicit — but do not mistake 2A-15 for proof that it fired.
    strict_before = await page.eval(JS_SCROLLBACK)
    res = await page.eval(f"({JS_CLICK_TRACE_BY_TEXT})('灰度先全量')")
    assert res == "clicked", f"late decision (strict) trace button: {res}"
    await asyncio.sleep(1.6)
    strict_parked = await page.eval(JS_SCROLLBACK)
    strict_max = (await page.eval(JS_SCROLL_METRICS))["maxScroll"]
    _record("2A-24 the strict-case jump scrolls nothing (assertion is meaningful)",
            strict_parked == strict_before
            and await page.eval(f"({JS_SEQ_IN_VIEW})(18)") is True,
            f"scrollTop {strict_before} -> {strict_parked} (unchanged), "
            f"maxScroll={strict_max}, seq18 already visible")

    c, _ = _http("POST", f"/api/internal/meetings/{mid}/asr-events",
                 _final(mid, SEED_SENTENCES + 3, "这条转写落在视口外，但跟随已暂停就不该抢滚动。", speaker_id=1))
    assert c == 200, f"strict final rejected: {c}"
    ok = await page.wait_for(f"{JS_SEQ_COUNT} === {SEED_SENTENCES + 3}", timeout=15)
    await asyncio.sleep(1.5)  # MUST let a smooth chase finish before sampling:
                              # sampling immediately passes even when the pause
                              # is missing, because the scroll is still in flight
    strict_after = await page.eval(JS_SCROLLBACK)
    _record("2A-25 ★§10 STRICT: a FINAL after a NO-SCROLL jump is not chased "
            "(the case only the explicit pause can cover)",
            ok and strict_after <= strict_before,
            f"scrollTop {strict_before} -> {strict_after} (max {strict_max}); "
            f"without pauseAutoFollow this reaches the new bottom")

    # back to auto-follow so 2A-19/20 start from a known-good state
    await page.eval("document.querySelector('.transcript-jump').click()")
    await asyncio.sleep(1.6)

    # ---- §6 dangling evidence -> light notice, no dialog -----------------
    res = await page.eval(f"({JS_CLICK_TRACE_BY_TEXT})('整理上线检查清单')")
    assert res == "clicked", f"dangling-evidence click: {res}"
    ok = await page.wait_for(
        "(() => { const n = document.querySelector('.transcript-note');"
        " return !!n && n.textContent.includes('暂不可定位') })()",
        timeout=6,
    )
    _record("2A-19 dangling evidence degrades to 「原始记录暂不可定位」", ok,
            f"note={(await page.eval(JS_NOTE))!r}")
    # and the transcript must still be usable afterwards
    body_after = await page.eval("document.body.innerText") or ""
    _record("2A-20 no error dialog / stack leaked while degrading",
            "Traceback" not in body_after and "MeetingStateCorrupt" not in body_after,
            "no internal detail in the DOM")

    # ---- UI-INTEL-1 regression: panel still updates WITHOUT a refresh ----
    # The two trailing FINALs are buffered (well under the 8-segment batch
    # threshold), so exactly one more full batch is needed for the state to
    # move. Fill it exactly: a partial batch would NOT advance the state and
    # the assertion below would fail for the wrong reason.
    live = await page.eval(JS_SEQ_COUNT)
    need = 0 if live % BATCH == 0 else BATCH - (live % BATCH)
    _send_finals(mid, live + 1, need)
    target_seq = live + need
    print(f"[e2e] sent {need} more FINAL to complete a batch (cursor target {target_seq})")

    state = _wait_state(mid, version=2, cursor=target_seq)
    _record("2A-21 one more full batch advanced the state (UI-INTEL-1 precondition)",
            state.get("version", 0) >= 2
            and state.get("last_processed_segment_seq", 0) >= target_seq,
            f"version={state.get('version')} cursor={state.get('last_processed_segment_seq')}")

    # The panel must have picked this up over the SAME /events connection —
    # no reload. `已理解 N 个版本` only renders when version > 0, so watch for
    # the version marker itself moving.
    ok = await page.wait_for(
        f"document.body.innerText.includes('已理解 {state.get('version')} 个版本')",
        timeout=25,
    )
    _record("2A-22 UI-INTEL-1 regression: panel reflected the update without a refresh",
            ok, f"expected version={state.get('version')}")

    # ---- transcript integrity untouched ----------------------------------
    _, segs = _http("GET", f"/api/meetings/{mid}/transcript")
    _record("2A-23 transcript unaffected by evidence navigation",
            [s["segment_seq"] for s in segs["segments"]] == list(range(1, target_seq + 1)),
            f"count={len(segs['segments'])} expected={target_seq}")

    await page.close()

    print(f"\nmeeting_id    = {mid}")
    print(f"state version = {state.get('version')}  cursor = {state.get('last_processed_segment_seq')}")
    print(f"\nTOTAL: {_PASS} PASS / {_FAIL} FAIL")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
