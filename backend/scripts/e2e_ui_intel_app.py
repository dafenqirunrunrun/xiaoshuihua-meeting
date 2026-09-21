#!/usr/bin/env python
"""Dev-only uvicorn entrypoint for the UI-INTEL-1 browser E2E.

Why this file exists
--------------------
The panel's *rendering* assertions (item text, section headers, the
`owner = null -> 待确认` fallback) need a Meeting State that actually HAS
content. The stock FakeProvider returns an intentionally EMPTY state update, so
driving the real chain with it can only ever prove the honest-empty path.

The alternative — writing a rich `state_json` straight into the scratch DB —
would bypass the very things this E2E is meant to exercise: the AGENT-1
pipeline, the commit-then-publish ordering, and the named SSE frame. So instead
we keep the WHOLE chain real and only change what the fake model "says":

    import app.agent.llm.fake as fake
    fake.DEFAULT_STATE_JSON = <rich, valid MeetingStateUpdate JSON>

That assignment is effective because `FakeProvider._default_for` resolves
`DEFAULT_STATE_JSON` from its module globals at CALL time.

This is a TEST HARNESS, not product code: it lives in `scripts/`, it is never
imported by the app, and it changes nothing on disk. `app/` is untouched.

Run (from `backend/`), with a scratch DB:

    DATABASE_URL=sqlite:///<tmp>/ui.db LLM_PROVIDER=fake \
      .venv/Scripts/python -m uvicorn scripts.e2e_ui_intel_app:app --port 8000
"""

from __future__ import annotations

import app.agent.llm.fake as fake

# A valid `MeetingStateUpdate` with content worth rendering AND with evidence
# wired so UI-INTEL-2A can be driven end to end:
#   - a topic + current_topic (so the panel's 当前议题 block renders)
#   - decisions:
#       * 下周三上线 cites [1,2]   -> near the TOP: 跳转必须向上滚
#       * 灰度先全量 cites [18,20] -> near the END: 跳转必须向下滚
#     (two decisions in OPPOSITE directions, because a scroll assertion that only
#      ever moves up — or only ever clamps at 0 — proves very little)
#   - one risk citing [8]            -> 单条证据
#   - two action items:
#       * 后端接口联调 cites [4,5]   -> 多证据：滚到 min=4 且 4、5 同时高亮
#       * 整理上线检查清单 cites [99] -> **悬空引用**（会议里没有第 99 句）
#         用于验证 §6「原始记录暂不可定位」的优雅降级
#     and deliberately omits owner AND deadline — the UI must render
#     负责人：待确认 / 截止时间：待确认 from the null, and the scratch DB must
#     still hold `null` afterwards (no write-back).
#   - one open question citing [7]
#   - a SECOND open question with `source_segment_seqs: []` -> the panel must not
#     render any「查看原文」entry for it (§6: no disabled dummy button either).
#
# NOTE on [99]: `source_segment_seqs` is MODEL-REPORTED, so a seq that does not
# exist in the transcript is a real, reachable state (AGENT-1 known limitation).
# Keeping it here means the E2E exercises the real degradation path rather than
# a defensive branch nobody hits.
RICH_STATE_JSON = (
    '{"current_topic": "上线时间安排",'
    ' "topics": [{"title": "上线时间安排",'
    '             "summary": "围绕上线时间与联调分工展开讨论。",'
    '             "source_segment_seqs": [1, 2, 3]}],'
    ' "decisions": [{"text": "下周三上线", "source_segment_seqs": [1, 2]},'
    '               {"text": "灰度先全量", "source_segment_seqs": [18, 20]}],'
    ' "action_items": [{"task": "后端接口联调", "owner": "小王", "deadline": "周二之前",'
    '                   "source_segment_seqs": [4, 5]},'
    '                  {"task": "整理上线检查清单", "owner": null, "deadline": null,'
    '                   "source_segment_seqs": [99]}],'
    ' "open_questions": [{"text": "是否需要灰度发布？", "source_segment_seqs": [7]},'
    '                   {"text": "监控告警阈值定多少？", "source_segment_seqs": []}],'
    ' "risks": [{"text": "支付接口稳定性需要确认", "source_segment_seqs": [8]}]}'
)

fake.DEFAULT_STATE_JSON = RICH_STATE_JSON

from app.main import app  # noqa: E402  (import AFTER the patch, on purpose)

__all__ = ["app"]
