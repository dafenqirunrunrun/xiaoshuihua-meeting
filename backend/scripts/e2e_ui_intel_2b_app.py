#!/usr/bin/env python
"""Dev-only uvicorn entrypoint for the UI-INTEL-2B browser E2E.

Why this file exists
--------------------
2B shows 「查看原文」 on the Result page's summary sections. That needs a
summary row whose `raw_model_output` is a real **V2 `FinalMeetingSummary`** —
and the stock FakeProvider's `DEFAULT_V2_JSON` has four EMPTY lists, so driving
the real chain with it can only ever prove the "no evidence entry rendered"
path (which is, awkwardly, also the legacy path).

Writing a V2 JSON straight into the scratch DB would be easier and would prove
less: it would bypass the AGENT-1 → evidence → AGENT-2 chain that actually
produces the seqs, and a fixture can't catch a write-path/read-path mismatch
(that mismatch is exactly what the shared `prune_code_fence` exists to prevent).
So the whole chain stays real and only what the fake model "says" changes:

    import app.agent.llm.fake as fake
    fake.DEFAULT_STATE_JSON = <rich MeetingStateUpdate JSON>
    fake.DEFAULT_V2_JSON     = <rich FinalMeetingSummary JSON>

Both assignments take effect because `FakeProvider._default_for` resolves those
module globals at CALL time.

The V2 output deliberately mirrors the state's citations, INCLUDING a dangling
one, but the dangling case has to be reachable from the UI — so the action item
that cites an impossible seq is given evidence `[4, 5, 999]`:

    4, 5   exist  -> must both highlight
    999    absent -> must be ignored (NOT reported as "cannot locate")

and a separate risk cites ONLY impossible seqs, to drive the all-missing
degradation to 「原始记录暂不可定位」.

This is a TEST HARNESS, not product code: it lives in `scripts/`, it is never
imported by the app (the app imports nothing from here; this module imports
*it*), and it changes nothing on disk. `app/` is untouched — the one extra
route below is registered on this module's copy of the ASGI app object only.

Run (from `backend/`), with a scratch DB:

    DATABASE_URL=sqlite:///<tmp>/ui2b.db LLM_PROVIDER=fake \
      .venv/Scripts/python -m uvicorn scripts.e2e_ui_intel_2b_app:app --port 8002
"""

from __future__ import annotations

import json

import app.agent.llm.fake as fake

# The meeting-state update AGENT-1 produces. Its seqs become the summary's
# evidence after AGENT-2 carries them through, so this is the real origin of
# every citation the Result page will offer to trace.
fake.DEFAULT_STATE_JSON = json.dumps(
    {
        "current_topic": "上线时间安排",
        "topics": [
            {
                "title": "上线时间安排",
                "summary": "围绕上线时间与联调分工展开讨论。",
                "source_segment_seqs": [1, 2, 3],
            }
        ],
        "decisions": [{"text": "下周三上线", "source_segment_seqs": [1, 2]}],
        "action_items": [
            {
                "task": "后端接口联调",
                "owner": "小王",
                "deadline": "周二之前",
                "source_segment_seqs": [4, 5],
            },
            # owner / deadline 缺失：UI 必须显示「待确认」，且**不得写回**
            {
                "task": "整理上线检查清单",
                "owner": None,
                "deadline": None,
                "source_segment_seqs": [6],
            },
        ],
        "open_questions": [{"text": "是否需要灰度发布？", "source_segment_seqs": [7]}],
        "risks": [{"text": "支付接口稳定性需要确认", "source_segment_seqs": [8]}],
    },
    ensure_ascii=False,
)

# NOTE: `status: null` on action items is not required by the schema and is
# omitted here on purpose — the projection must not depend on it.
#
# `closing_summary` empty: the model is allowed to omit prose it has nothing to
# say about, and an empty string is the legal expression of that.
fake.DEFAULT_V2_JSON = json.dumps(
    {
        "overview": "会议确定了上线时间与联调分工，并留有两个待确认项。",
        "key_topics": [
            {"title": "上线时间安排", "summary": "围绕上线时间展开。", "source_segment_seqs": [1]}
        ],
        "decisions": [{"text": "下周三上线", "source_segment_seqs": [1, 2]}],
        "action_items": [
            # PARTIAL dangling: 4 and 5 exist (both must highlight), 999 does not
            # (must be ignored, NOT treated as "cannot locate").
            {
                "task": "后端接口联调",
                "owner": "小王",
                "deadline": "周二之前",
                "source_segment_seqs": [4, 5, 999],
            },
            # owner / deadline null -> the UI renders 待确认 from the null
            {
                "task": "整理上线检查清单",
                "owner": None,
                "deadline": None,
                "source_segment_seqs": [6],
            },
        ],
        "open_questions": [{"text": "是否需要灰度发布？", "source_segment_seqs": [7]}],
        # ALL-missing evidence: every cited seq is absent from the transcript, so
        # this is the one item that must degrade to 「原始记录暂不可定位」.
        "risks": [{"text": "支付接口稳定性需要确认", "source_segment_seqs": [901, 902]}],
        "closing_summary": "",
    },
    ensure_ascii=False,
)

from app.main import app  # noqa: E402  (import AFTER the patch, on purpose)

# ---------------------------------------------------------------------------
# Dev-only shape presets — how the NEGATIVE CONTROL is produced
# ---------------------------------------------------------------------------
# Case 5 (legacy / fallback summary → no evidence entry) cannot be produced by
# just "sending too few FINALs". `finalize_for_meeting` force-flushes whatever
# is pending before generating (§2 ordering), so a meeting state row always
# exists by then and the V2 branch always runs — with this harness loaded, every
# meeting would yield a V2 summary.
#
# So the control switches what the fake "says" in a way that drives a REAL
# documented degradation: an EMPTY state update makes `assess_usability` return
# `state_empty`, which is §9's designed fallback to the full-transcript legacy
# path. That prompt embeds the transcript and carries neither delimiter, so
# `_default_for` answers with the LEGACY shape — a populated legacy summary with
# `summary_evidence = null`. That is the honest way to reach Case 5: the
# frontend gets a real summary with real content and must render no entry.
#
# The switch is an explicit preset rather than a raw JSON override so the
# fixtures live in exactly ONE place (the two rich blobs above) and a test can
# never drift into a third, hand-written shape. The suite selects a preset at
# the start of each phase instead of assuming whatever the process happens to
# hold — the process outlives a single run, so "leave it as it was" is not a
# usable default.
#
# This route exists only in this harness module, which the app never imports
# (the direction is reversed: this module imports the app); it is not part of
# any contract, is excluded from the OpenAPI schema, and is registered on this
# module's copy of the ASGI app object only.
_PRESETS: dict[str, dict[str, object]] = {
    "v2": {
        "state": json.loads(fake.DEFAULT_STATE_JSON),
        "v2": json.loads(fake.DEFAULT_V2_JSON),
    },
    "empty_state": {
        "state": {
            "current_topic": None,
            "topics": [],
            "decisions": [],
            "action_items": [],
            "open_questions": [],
            "risks": [],
        },
        "v2": json.loads(fake.DEFAULT_V2_JSON),
    },
}


@app.post("/_dev/fake-preset", include_in_schema=False)
def _set_fake_preset(payload: dict) -> dict:
    """Select which shape the FakeProvider answers with (dev E2E only)."""
    name = payload.get("preset")
    preset = _PRESETS.get(name)
    if preset is None:
        return {"ok": False, "error": f"unknown preset: {name!r}", "known": sorted(_PRESETS)}
    fake.DEFAULT_STATE_JSON = json.dumps(preset["state"], ensure_ascii=False)
    fake.DEFAULT_V2_JSON = json.dumps(preset["v2"], ensure_ascii=False)
    return {"ok": True, "preset": name}


__all__ = ["app"]
