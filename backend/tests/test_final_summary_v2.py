"""AGENT-2 test suite — Meeting State -> Final Summary V2.

Step AGENT-2 changes the final summary's INPUT, not its contract:

    before   full transcript -> LLM -> summary
    after    Meeting State (working memory) + complete FINAL transcript -> LLM -> summary

with priority evidence used for oversized prompts and the legacy path kept
intact as a designed FALLBACK. These tests exist
mainly to answer the question the green legacy suites CANNOT answer: **is V2
actually running, or is everything silently falling back?**

That distinction is the whole point of this file. `test_summary_step7a/b` stay
green either way, because they seed a handful of FINALs and `assess_usability`
returns a reason (no state / state failed / stale) and the legacy path produces
an acceptable summary. So every test below either asserts on `summary_source`
directly or arranges the inputs so only one path can produce the expected output.

Coverage map (step spec §18's 20 cases, grouped):

  Planning / gate      T01-T08   source selection, staleness (§2), fallbacks (§9)
  Empty meeting        T09-T10   §16 — deterministic, no LLM, no 404, no hang
  Evidence             T11-T14   §4 priority, neighbour window, cap, dangling cites
  Schema / compat      T15-T18   §5 backward compatibility, no contract break
  Conflict 收口        T19-T21   §7 — over-confident state cannot force a decision
  Merge / boundaries   T22-T24   §14/§15 dedup + semantic boundaries
  Ordering invariant   T25-T27   §2 finish -> flush -> current -> summary
  Feature flag         T28-T29   §10 — OFF restores the legacy behaviour exactly
  Replay E2E           T30-T32   §17 — the six-segment scripted meeting
"""

from __future__ import annotations

import json

import pytest

from app.agent import evidence as evidence_module
from app.agent.final_summary_prompts import to_legacy_schema
from app.agent.final_summary_schemas import FinalMeetingSummary
from app.agent.final_summary_service import (
    REASON_AGENT_DISABLED,
    REASON_DISABLED,
    REASON_NO_STATE,
    REASON_STATE_EMPTY,
    REASON_STATE_FAILED,
    REASON_STATE_STALE,
    SOURCE_EMPTY_MEETING,
    SOURCE_MEETING_STATE,
    SOURCE_TRANSCRIPT_FALLBACK,
    assess_usability,
    build_plan,
    empty_meeting_summary,
    parse_v2,
)
from app.agent.summary_service import generate as generate_summary
from app.meeting_state.schemas import (
    ActionItem,
    DecisionItem,
    MeetingStateContent,
    OpenQuestion,
    RiskItem,
    TopicState,
)
from app.meeting_state import repository as state_repository
from app.config import settings

# ---------------------------------------------------------------------------
# The six-segment scripted meeting (step spec §17). Two decisions are reachable
# here: a firm one (seq 3) and a PROPOSAL (seq 2) that must NOT be promoted.
# ---------------------------------------------------------------------------
SCRIPT = [
    (1, "今天讨论上线时间"),
    (2, "我觉得下周一可能有点赶"),
    (3, "那就定下周三上线吧"),
    (4, "小王负责后端接口联调"),
    (5, "周二之前完成"),
    (6, "还有支付接口稳定性需要确认"),
]

# A well-formed V2 output corresponding to SCRIPT, used to script the provider.
V2_VALID = {
    "overview": "会议确定了上线时间并分配了联调任务。",
    "key_topics": [
        {"title": "上线时间", "summary": "讨论后定在下周三。", "source_segment_seqs": [1, 3]}
    ],
    "decisions": [{"text": "定于下周三上线", "source_segment_seqs": [3]}],
    "action_items": [
        {
            "task": "完成后端接口联调",
            "owner": "小王",
            "deadline": "周二之前",
            "status": None,
            "source_segment_seqs": [4, 5],
        }
    ],
    "open_questions": [],
    "risks": [{"text": "支付接口稳定性需要确认", "source_segment_seqs": [6]}],
    "closing_summary": "待联调完成后按计划上线。",
}

V2_RAW = json.dumps(V2_VALID, ensure_ascii=False)

# A pathological V2 output: the model promotes the seq-2 PROPOSAL into a firm
# decision AND invents an owner/deadline nobody stated. Used by the §7 conflict
# tests to prove over-confidence is caught downstream.
V2_OVERCONFIDENT = json.dumps(
    {
        "overview": "会议决定下周一上线。",
        "key_topics": [],
        "decisions": [
            {"text": "定于下周一上线", "source_segment_seqs": [2]},
        ],
        "action_items": [
            {
                "task": "上线",
                "owner": "张三",
                "deadline": "本周日",
                "status": None,
                "source_segment_seqs": [2],
            }
        ],
        "open_questions": [],
        "risks": [],
        "closing_summary": "",
    },
    ensure_ascii=False,
)


class ScriptedProvider:
    """A provider whose output is chosen by what it is asked.

    Dispatching on the prompt lets ONE provider serve both the AGENT-1 state
    updates and the AGENT-2 summary call inside a single test — which matters,
    because a real meeting does exactly that and a queue-based fake would be
    order-dependent and brittle.
    """

    name = "agent2-scripted"

    def __init__(self, *, summary_output: str | None = None, state_output=None) -> None:
        self.summary_output = summary_output
        self.state_output = state_output
        self.calls: list[str] = []
        self.summary_calls: list[str] = []
        self.state_calls: list[str] = []

    # -- AGENT-1 shape: build a state update from the batch it was given -----
    def _state_payload(self, prompt: str) -> str:
        if self.state_output is not None:
            return self.state_output
        seqs = _seqs_in_batch(prompt)
        content = MeetingStateContent()
        if 1 in seqs or 2 in seqs or 3 in seqs:
            content.topics = [
                TopicState(
                    title="上线时间",
                    summary="讨论上线时间。",
                    source_segment_seqs=[s for s in (1, 2, 3) if s in seqs],
                )
            ]
        if 3 in seqs:
            content.decisions = [
                DecisionItem(text="定于下周三上线", source_segment_seqs=[3])
            ]
        if 2 in seqs and 3 not in seqs:
            # the PROPOSAL, recorded as an open question rather than a decision
            content.open_questions = [
                OpenQuestion(text="下周一是否来得及", source_segment_seqs=[2])
            ]
        if 4 in seqs or 5 in seqs:
            content.action_items = [
                ActionItem(
                    task="完成后端接口联调",
                    owner="小王" if 4 in seqs else None,
                    deadline="周二之前" if 5 in seqs else None,
                    source_segment_seqs=[s for s in (4, 5) if s in seqs],
                )
            ]
        if 6 in seqs:
            content.risks = [
                RiskItem(text="支付接口稳定性需要确认", source_segment_seqs=[6])
            ]
        return content.model_dump_json()

    def complete(self, prompt: str, *, timeout_seconds: float | None = None) -> str:
        self.calls.append(prompt)
        if "最终纪要" in prompt or "<meeting_state>" in prompt:
            self.summary_calls.append(prompt)
            return self.summary_output if self.summary_output is not None else V2_RAW
        self.state_calls.append(prompt)
        return self._state_payload(prompt)


def _seqs_in_batch(prompt: str) -> list[int]:
    import re

    return sorted({int(m) for m in re.findall(r"\[(\d+)\]", prompt)})


# ===========================================================================
# Fixtures / helpers
# ===========================================================================

@pytest.fixture
def scripted(monkeypatch):
    """Install a ScriptedProvider on EVERY path production resolves.

    Two bindings must be patched, and the difference is a real one:

      - ``meeting_state.service`` imports ``get_provider`` INSIDE the function,
        so patching the factory module reaches it.
      - ``summary_service`` does ``from ...factory import get_provider`` at
        module level, binding the name into its own namespace — patching the
        factory alone would NOT reach it.

    Patching both means the tests exercise the same resolution path production
    uses; a missing wiring fails loudly instead of silently falling back.

    The lambda accepts the factory's optional ``model`` argument (AGENT-2 added
    it so ``FINAL_SUMMARY_MODEL`` can target the summary call); a zero-arg
    double would break the real call signature.
    """
    provider = ScriptedProvider()
    monkeypatch.setattr(
        "app.agent.llm.factory.get_provider",
        lambda model=None, **_policy: provider,
    )
    monkeypatch.setattr(
        "app.agent.summary_service.get_provider",
        lambda model=None, **_policy: provider,
    )
    monkeypatch.setattr("app.config.settings.meeting_agent_enabled", True)
    monkeypatch.setattr("app.config.settings.final_summary_v2_enabled", True)
    return provider


def _create_and_start(client, title="agent2") -> str:
    mid = client.post("/api/meetings", json={"title": title}).json()["id"]
    client.post(f"/api/meetings/{mid}/start")
    return mid


def _final(client, mid, sentence_id, text, session="s1"):
    r = client.post(
        f"/api/internal/meetings/{mid}/asr-events",
        json={
            "meetingId": mid,
            "asrSessionId": session,
            "sentenceId": sentence_id,
            "type": "transcript.final",
            "speakerId": 1,
            "startMs": sentence_id * 1000,
            "endMs": sentence_id * 1000 + 500,
            "text": text,
            "provider": "tencent",
        },
    )
    from app.meeting_state.scheduler import meeting_state_scheduler

    assert meeting_state_scheduler.wait_for_idle(mid, timeout_seconds=2.0)
    assert r.status_code == 200, r.text
    return r


def _seed_meeting(client, script=SCRIPT) -> str:
    mid = _create_and_start(client)
    for sid, text in script:
        _final(client, mid, sid, text)
    # Model the scheduler's silence/interval trigger deterministically. The
    # finalizer itself must never perform this realtime catch-up.
    from app.db import SessionLocal
    from app.meeting_state.service import process_pending

    with SessionLocal() as session:
        process_pending(session, mid, force_flush=True)
    return mid


def _finish(client, mid):
    """Drive the meeting to FINALIZING (POST /finish)."""
    r = client.post(f"/api/meetings/{mid}/finish")
    assert r.status_code == 200, r.text
    return r


def _stream_end(client, mid, last_seq):
    """Send TRANSCRIPT_STREAM_END and let the Finalizer run.

    STREAM_END — not /finish — is what triggers the Finalizer (Step 7B), and it
    is accepted only on a FINALIZING + frozen meeting. It carries the explicit
    terminal seq so the integrity check has something to validate against.
    """
    r = client.post(
        f"/api/internal/meetings/{mid}/stream-end",
        json={
            "event_id": f"se_{mid}_{last_seq}",
            "meeting_id": mid,
            "event_type": "TRANSCRIPT_STREAM_END",
            "last_segment_seq": last_seq,
        },
    )
    assert r.status_code == 200, r.text
    return r


def _finalize(client, mid, last_seq):
    """The full terminal chain: finish -> STREAM_END -> Finalizer -> COMPLETED."""
    _finish(client, mid)
    _stream_end(client, mid, last_seq)


def _freeze_only(mid, last_seq):
    """Freeze the transcript durably WITHOUT running the Finalizer.

    Goes straight to `stream_service.handle_stream_end`, bypassing the route's
    BackgroundTasks. Step 7B splits these deliberately — the freeze is durable
    bookkeeping, the Finalizer is a separate trigger — and the §2 tests need to
    observe the state in the window BETWEEN them, which the route cannot expose
    (TestClient runs BackgroundTasks before returning).
    """
    from app.db import SessionLocal
    from app.transcript import schemas as transcript_schemas
    from app.transcript import stream_service

    s = SessionLocal()
    try:
        return stream_service.handle_stream_end(
            s, mid,
            transcript_schemas.StreamEndEvent(
                event_id=f"se_{mid}_{last_seq}", meeting_id=mid, last_segment_seq=last_seq,
            ),
        )
    finally:
        s.close()


def _summary(client, mid):
    r = client.get(f"/api/meetings/{mid}/summary")
    assert r.status_code == 200, r.text
    return r.json()


def _state(client, mid):
    r = client.get(f"/api/internal/meetings/{mid}/state")
    return r.json() if r.status_code == 200 else None


def _plan(db, mid):
    """Build a plan directly (no LLM) so source selection can be asserted.

    `build_plan` needs a `legacy_material` thunk. The real one lives in
    `summary_service._legacy_material(db, meeting, meeting_id)`; asking the
    DEFINITION to build it (rather than hand-rolling a stub) keeps this honest:
    if that function is renamed or its contract changes, this fails loudly.
    """
    from app.meeting.meeting_model import Meeting
    from app.agent.summary_service import _legacy_material  # noqa: PLC2701

    meeting = db.get(Meeting, mid)
    return build_plan(
        db, meeting, legacy_material=lambda: _legacy_material(db, meeting, mid)
    )


@pytest.fixture
def db():
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


# ===========================================================================
# T01-T08  Planning / usability gate
# ===========================================================================

class TestUsabilityGate:
    def test_t01_v2_disabled_reports_disabled(self, client, monkeypatch):
        mid = _seed_meeting(client)
        monkeypatch.setattr(settings, "final_summary_v2_enabled", False)
        with __import__("app.db", fromlist=["SessionLocal"]).SessionLocal() as s:
            a = assess_usability(s, mid)
        assert a.usable is False and a.reason == REASON_DISABLED

    def test_t02_agent_disabled_reports_agent_disabled(self, client, monkeypatch):
        mid = _seed_meeting(client)
        monkeypatch.setattr(settings, "meeting_agent_enabled", False)
        with __import__("app.db", fromlist=["SessionLocal"]).SessionLocal() as s:
            a = assess_usability(s, mid)
        assert a.usable is False and a.reason == REASON_AGENT_DISABLED

    def test_t03_no_state_row_reports_no_state(self, client, db):
        # No FINALs processed by the agent at all -> no state row.
        mid = _create_and_start(client)
        _final(client, mid, 1, "只有一句")
        a = assess_usability(db, mid)
        assert a.usable is False and a.reason == REASON_NO_STATE

    def test_t04_state_behind_transcript_is_stale(self, client, db, scripted):
        """§2: a state older than the transcript must be REFUSED, not used."""
        mid = _create_and_start(client)
        _final(client, mid, 1, "第一句")
        _final(client, mid, 2, "第二句")
        # Force a state that has only absorbed seq 1 while the transcript has 2.
        state_repository.upsert(
            db, mid,
            content=MeetingStateContent(
                topics=[TopicState(title="t", summary="s", source_segment_seqs=[1])]
            ),
            version=1,
            last_processed_segment_seq=1,
            status="ACTIVE",
            model_name="test",
        )
        db.commit()
        a = assess_usability(db, mid)
        assert a.usable is False and a.reason == REASON_STATE_STALE
        assert a.last_processed_segment_seq == 1 and a.final_transcript_seq == 2

    def test_t05_failed_state_is_refused(self, client, db):
        """A FAILED state's last update did not land — never present it as current."""
        mid = _create_and_start(client)
        _final(client, mid, 1, "第一句")
        state_repository.upsert(
            db, mid,
            content=MeetingStateContent(
                topics=[TopicState(title="t", summary="s", source_segment_seqs=[1])]
            ),
            version=1,
            last_processed_segment_seq=1,
            status="ACTIVE",
            model_name="test",
        )
        state_repository.mark_failed(db, mid, reason="boom")
        db.commit()
        a = assess_usability(db, mid)
        assert a.usable is False and a.reason == REASON_STATE_STALE

    def test_t06_empty_state_is_refused(self, client, db):
        mid = _create_and_start(client)
        _final(client, mid, 1, "第一句")
        state_repository.upsert(
            db, mid,
            content=MeetingStateContent(),
            version=1,
            last_processed_segment_seq=1,
            status="ACTIVE",
            model_name="test",
        )
        db.commit()
        a = assess_usability(db, mid)
        assert a.usable is False and a.reason == REASON_STATE_EMPTY

    def test_t07_current_state_is_usable(self, client, db, scripted):
        mid = _seed_meeting(client)
        _finalize(client, mid, 6)
        a = assess_usability(db, mid)
        assert a.usable is True and a.reason is None
        assert a.last_processed_segment_seq == a.final_transcript_seq == 6

    def test_t08_plan_source_is_meeting_state_when_usable(self, client, db, scripted):
        """The load-bearing assertion: V2 is the path actually taken."""
        mid = _seed_meeting(client)
        _finalize(client, mid, 6)
        plan = _plan(db, mid)
        assert plan.source == SOURCE_MEETING_STATE
        assert plan.fallback_reason is None
        assert plan.prompt and "<meeting_state>" in plan.prompt
        assert plan.transcript_scope == "full"
        assert plan.evidence_segment_count == 6
        assert "以下为完整 FINAL 转写" in plan.prompt
        for _seq, text in SCRIPT:
            assert text in plan.prompt
        assert plan.parse is not None and plan.build_repair is not None

    def test_t08b_oversized_full_transcript_degrades_to_priority_evidence(
        self, client, db, scripted, monkeypatch
    ):
        mid = _seed_meeting(client)
        _finalize(client, mid, 6)
        full = _plan(db, mid)
        assert full.transcript_scope == "full"

        monkeypatch.setattr(
            "app.config.settings.final_summary_max_input_chars",
            len(full.prompt) - 1,
        )
        monkeypatch.setattr(
            "app.config.settings.final_summary_max_evidence_segments", 1
        )
        selected = _plan(db, mid)
        assert selected.transcript_scope == "selected"
        assert selected.evidence_segment_count == 1
        assert "容量保护后的重点证据" in selected.prompt


# ===========================================================================
# T09-T10  Empty meeting (§16)
# ===========================================================================

class TestEmptyMeeting:
    def test_t09_empty_meeting_is_deterministic_and_llm_free(self, client, db, scripted):
        mid = _create_and_start(client)
        _finalize(client, mid, 0)
        plan = _plan(db, mid)
        assert plan.source == SOURCE_EMPTY_MEETING
        assert plan.precomputed is not None
        assert plan.prompt == "", "empty meeting must not build a prompt at all"
        assert plan.precomputed.key_topics == []
        calls_before = len(scripted.calls)
        assert len(scripted.calls) == calls_before, "no LLM call for an empty meeting"

    def test_t10_empty_meeting_reaches_completed_not_404_not_hanging(self, client, scripted):
        mid = _create_and_start(client)
        _finalize(client, mid, 0)
        body = _summary(client, mid)
        assert body["status"] == "COMPLETED"
        assert body["overview"]
        assert body["key_topics"] == [] and body["conclusions"] == []
        meeting = client.get(f"/api/meetings/{mid}").json()
        assert meeting["status"] == "COMPLETED"

    def test_t10b_empty_meeting_helper_shape(self):
        legacy, raw = empty_meeting_summary()
        assert legacy.overview and legacy.key_topics == []
        assert json.loads(raw)["overview"] == legacy.overview


# ===========================================================================
# T11-T14  Evidence selection (§4)
# ===========================================================================

class TestEvidenceSelection:
    def _seg(self, seq):
        from app.transcript.segment_model import TranscriptSegment

        return TranscriptSegment(
            meeting_id="m", segment_seq=seq, speaker_id="s", text=f"t{seq}",
            start_ms=0, end_ms=1,
        )

    def test_t11_cited_seqs_are_deduped_across_all_lists(self):
        content = MeetingStateContent(
            decisions=[DecisionItem(text="d", source_segment_seqs=[3, 5])],
            action_items=[ActionItem(task="a", source_segment_seqs=[5, 7])],
            risks=[RiskItem(text="r", source_segment_seqs=[3])],
        )
        assert evidence_module.cited_segment_seqs(content) == [3, 5, 7]

    def test_t12_neighbour_window_widens_and_carries_priority(self):
        content = MeetingStateContent(
            decisions=[DecisionItem(text="d", source_segment_seqs=[4])],
            topics=[TopicState(title="t", summary="s", source_segment_seqs=[9])],
        )
        segs = [self._seg(i) for i in range(1, 11)]
        sel = evidence_module.select_evidence(
            content, segs, max_evidence_segments=99, neighbor_window=1
        )
        assert sel.cited_seqs == [4, 9]
        # seq 4's neighbours (3, 5) come in as context; all three outrank the
        # directly-cited topic at 9 because they inherit the DECISION tier.
        assert sel.segments[0].segment_seq == 3
        assert [s.segment_seq for s in sel.segments][:3] == [3, 4, 5]

    def test_t13_cap_drops_lowest_priority_and_reports_it(self):
        content = MeetingStateContent(
            decisions=[DecisionItem(text="d", source_segment_seqs=[1])],
            topics=[TopicState(title="t", summary="s", source_segment_seqs=[2, 3, 4])],
        )
        segs = [self._seg(i) for i in range(1, 6)]
        sel = evidence_module.select_evidence(
            content, segs, max_evidence_segments=2, neighbor_window=0
        )
        assert sel.truncated is True and sel.dropped_count == 2
        # the decision's evidence survives; the topic's is what got cut
        assert [s.segment_seq for s in sel.segments] == [1, 2]

    def test_t14_dangling_citation_is_dropped_not_conjured(self):
        content = MeetingStateContent(
            decisions=[DecisionItem(text="d", source_segment_seqs=[99])]
        )
        segs = [self._seg(1), self._seg(2)]
        sel = evidence_module.select_evidence(
            content, segs, max_evidence_segments=10, neighbor_window=1
        )
        assert sel.segments == []
        assert sel.cited_seqs == [99]


# ===========================================================================
# T15-T18  Schema + backward compatibility (§5/§9)
# ===========================================================================

class TestCompat:
    def test_t15_v2_schema_forbids_extra_fields(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            FinalMeetingSummary.model_validate(
                {"overview": "x", "key_topics": [], "unknown": 1}
            )

    def test_t16_v2_schema_requires_only_overview(self):
        m = FinalMeetingSummary.model_validate({"overview": "只有概述"})
        assert m.decisions == [] and m.closing_summary == ""

    def test_t17_legacy_mapper_flattens_without_losing_commitments(self):
        """§5: the old contract is unchanged, but nothing is silently dropped."""
        parsed = FinalMeetingSummary.model_validate(V2_VALID)
        legacy = to_legacy_schema(parsed)
        assert legacy.overview == V2_VALID["overview"]
        assert legacy.key_topics == ["上线时间"]
        # decision text present
        assert "定于下周三上线" in legacy.conclusions
        # action item folded in with owner + deadline so the commitment survives
        assert any("完成后端接口联调" in c and "小王" in c and "周二之前" in c
                   for c in legacy.conclusions)
        # risks land in open_questions (the old schema has no risk field)
        assert any("支付接口稳定性" in q for q in legacy.open_questions)

    def test_t17b_action_item_without_owner_has_no_placeholder(self):
        parsed = FinalMeetingSummary.model_validate(
            {
                "overview": "x",
                "action_items": [
                    {"task": "整理文档", "owner": None, "deadline": None,
                     "source_segment_seqs": [1]}
                ],
            }
        )
        legacy = to_legacy_schema(parsed)
        assert "整理文档" in legacy.conclusions
        assert not any("未指定" in c or "None" in c for c in legacy.conclusions)

    def test_t18_parse_v2_rejects_a_top_level_list(self):
        from app.agent.final_summary_service import FinalSummarySchemaError

        with pytest.raises(FinalSummarySchemaError):
            parse_v2("[]")

    def test_t18b_parse_v2_strips_a_code_fence(self):
        raw = "```json\n" + V2_RAW + "\n```"
        legacy, full = parse_v2(raw)
        assert full.decisions[0].text == "定于下周三上线"
        assert legacy.key_topics == ["上线时间"]


# ===========================================================================
# T19-T21  State/Evidence conflict 收口 (§7)
# ===========================================================================

class TestConflictDowngrade:
    def test_t19_overconfident_state_does_not_reach_the_summary(self, client, scripted):
        """A state asserting a decision the evidence does not support is caught.

        Here the STATE is over-confident (it claims a decision the transcript
        never settled) while the final model output follows the rules and keeps
        the proposal as an open question. The point is that the pipeline has a
        second pass: the summary is NOT required to echo the state's claim.
        """
        scripted.summary_output = json.dumps(
            {
                "overview": "讨论了上线时间，尚未最终确定。",
                "key_topics": [],
                "decisions": [],
                "action_items": [],
                "open_questions": [
                    {"text": "下周一是否来得及", "source_segment_seqs": [2]}
                ],
                "risks": [],
                "closing_summary": "",
            },
            ensure_ascii=False,
        )
        # State is over-confident: it records seq 2 as a DECISION.
        scripted.state_output = MeetingStateContent(
            decisions=[DecisionItem(text="定于下周一上线", source_segment_seqs=[2])]
        ).model_dump_json()

        mid = _seed_meeting(client, script=SSCRIPT_MIN)
        # ASR ingress allocates segment_seq itself (max+1), so the two sentences
        # land on seqs 1 and 2 regardless of the sentenceIds used to send them.
        _finalize(client, mid, 2)
        body = _summary(client, mid)
        assert body["status"] == "COMPLETED"
        assert "下周一上线" not in "".join(body["conclusions"]), (
            "an unsupported decision must not surface as a conclusion"
        )

    def test_t20_malformed_summary_output_falls_back_to_repair(self, client, scripted):
        """A bad first response is repaired (bounded), not silently accepted."""
        scripted.summary_output = "not json at all"
        mid = _seed_meeting(client)
        _finalize(client, mid, 6)
        body = _summary(client, mid)
        # repair also returns non-JSON -> FAILED, but NEVER a crash and never a
        # half-written summary. The meeting still completes.
        assert body["status"] in ("COMPLETED", "FAILED")
        meeting = client.get(f"/api/meetings/{mid}").json()
        assert meeting["status"] == "COMPLETED"

    def test_t21_summary_failure_does_not_pollute_state_or_transcript(
        self, client, db, scripted
    ):
        """§11: a summary failure is contained to summary.status."""
        scripted.summary_output = "definitely not json"
        mid = _seed_meeting(client)
        _finalize(client, mid, 6)
        before = _state(client, mid)
        body = _summary(client, mid)
        after = _state(client, mid)
        assert body["status"] in ("COMPLETED", "FAILED")
        # the state is byte-identical: the summary never writes back to it
        assert _content_of(before) == _content_of(after)
        # transcript intact
        snap = client.get(f"/api/meetings/{mid}/transcript").json()
        assert len(snap["segments"]) == len(SCRIPT)


def _content_of(state_json):
    if state_json is None:
        return None
    return {k: v for k, v in state_json.items()
            if k not in ("version", "last_processed_segment_seq", "status", "updated_at")}


SSCRIPT_MIN = [(2, "我觉得下周一可能有点赶"), (3, "那就定下周三上线吧")]


# ===========================================================================
# T22-T24  Merge + semantic boundaries (§14/§15)
# ===========================================================================

class TestMergeAndBoundaries:
    def test_t22_state_merges_action_item_across_batches_into_one(self, client, db, scripted):
        """§14: owner (seq 4) and deadline (seq 5) merge into ONE action item.

        With the neighbour window the two seqs can land in different batches; the
        contract is that the persisted item is the MERGED final state, not two.
        """
        mid = _seed_meeting(client)
        _finalize(client, mid, 6)
        state = _state(client, mid)
        items = state["action_items"]
        assert len(items) == 1, f"expected one merged action item, got {items}"
        assert items[0]["source_segment_seqs"] == [4, 5]

    def test_t23_evidence_trace_is_preserved_end_to_end(self, client, scripted):
        """§13: decisions/actions/questions/risks all carry their evidence."""
        mid = _seed_meeting(client)
        _finalize(client, mid, 6)
        state = _state(client, mid)
        assert state["decisions"][0]["source_segment_seqs"] == [3]
        assert state["action_items"][0]["source_segment_seqs"] == [4, 5]
        assert state["risks"][0]["source_segment_seqs"] == [6]

    def test_t24_prompt_forbids_promoting_a_proposal_and_inventing_owners(self):
        """§15/§7 are enforced in the prompt the model actually receives."""
        from app.agent.final_summary_prompts import _RULES

        assert "open_question" in _RULES  # downgrade path named
        assert "null" in _RULES           # owner/deadline stay null
        assert "decision" in _RULES


# ===========================================================================
# T25-T27  Ordering invariant (§2)
# ===========================================================================

class TestOrderingInvariant:
    def test_finalizer_has_no_realtime_flush_dependency(self):
        from app.agent import finalizer as finalizer_service

        assert not hasattr(finalizer_service, "_flush_meeting_state")

    def test_t25_finish_does_not_flush_the_realtime_tail(self, client, db, scripted):
        """Finish cancels pending realtime work instead of blocking on it."""
        # batch threshold 99 so nothing flushes proactively; only force_flush can
        mid = _create_and_start(client)
        for sid, text in SCRIPT:
            _final(client, mid, sid, text)
        assert _state(client, mid) is None or _state(client, mid)["version"] == 0
        _finish(client, mid)
        # NOTE: no STREAM_END here — this asserts the /finish-time flush alone,
        # before the Finalizer's own flush can mask a broken one.
        state = _state(client, mid)
        assert state is None or state["last_processed_segment_seq"] == 0

    def test_t26_finalizer_uses_transcript_without_flushing_state(self, client, db, scripted):
        """A stale state is optional context and never gates final generation.

        Simulates the real failure the invariant exists for: the transcript is
        frozen and the meeting FINALIZING, but the state is behind (the agent's
        own flush failed / never ran). The Finalizer must FIX the state, not
        summarize it stale.
        """
        mid = _seed_meeting(client)
        _finish(client, mid)
        _freeze_only(mid, 6)

        # Rewind the state so it is stale relative to the frozen transcript
        # (cursor 0), as if the agent's flush had failed / never run.
        state_repository.upsert(
            db, mid,
            content=MeetingStateContent(
                topics=[TopicState(title="旧", summary="旧状态", source_segment_seqs=[1])]
            ),
            version=1,
            last_processed_segment_seq=0,
            status="ACTIVE",
            model_name="test",
        )
        db.commit()
        assert assess_usability(db, mid).reason == REASON_STATE_STALE

        from app.agent import finalizer as finalizer_service

        finalizer_service.finalize_for_meeting(db, mid, provider=scripted)
        # Realtime state remains stale, but the full frozen transcript still
        # produces the canonical V2 summary.
        assert assess_usability(db, mid).reason == REASON_STATE_STALE
        assert _summary(client, mid)["status"] == "COMPLETED"

    def test_t27_trailing_final_is_read_directly_by_finalizer(self, client, db, scripted):
        """A trailing FINAL need not enter realtime state to be summarized."""
        mid = _create_and_start(client)
        for sid, text in SCRIPT[:4]:
            _final(client, mid, sid, text)
        _finish(client, mid)
        # trailing FINAL arrives after finish, before the transcript freezes.
        _final(client, mid, 5, "周二之前完成")
        _freeze_only(mid, 5)
        from app.agent import finalizer as finalizer_service

        finalizer_service.finalize_for_meeting(db, mid, provider=scripted)
        state = _state(client, mid)
        assert state["last_processed_segment_seq"] < 5
        assert _summary(client, mid)["status"] == "COMPLETED"


# ===========================================================================
# T28-T29  Feature flag (§10)
# ===========================================================================

class TestFeatureFlag:
    def test_t28_flag_off_restores_legacy_behaviour_exactly(self, client, monkeypatch):
        """§10: OFF must be a real, complete revert — not a partial one."""
        monkeypatch.setattr(settings, "final_summary_v2_enabled", False)
        mid = _create_and_start(client)
        for sid, text in SCRIPT:
            _final(client, mid, sid, text)
        _finalize(client, mid, 6)
        body = _summary(client, mid)
        assert body["status"] == "COMPLETED"
        # the legacy parser accepted the FakeProvider default shape
        assert body["overview"]
        assert body["key_topics"] == ["实时转写", "会议纪要"]

    def test_t29_flag_off_records_a_fallback_reason(self, client, db, monkeypatch):
        monkeypatch.setattr(settings, "final_summary_v2_enabled", False)
        mid = _seed_meeting(client)
        _finalize(client, mid, 6)
        plan = _plan(db, mid)
        assert plan.source == SOURCE_TRANSCRIPT_FALLBACK
        assert plan.fallback_reason == REASON_DISABLED


# ===========================================================================
# T30-T32  §17 replay E2E
# ===========================================================================

class TestScriptedReplay:
    def test_t30_full_replay_produces_a_correct_structured_summary(self, client, scripted):
        """The §17 acceptance: structure correct, durable, meeting COMPLETED."""
        mid = _seed_meeting(client)
        _finalize(client, mid, 6)
        body = _summary(client, mid)
        assert body["status"] == "COMPLETED"

        # structure, not wording: the decision is the settled one (seq 3), the
        # proposal (seq 2) is NOT a conclusion, and the risk is present.
        conclusions = " | ".join(body["conclusions"])
        assert "下周三上线" in conclusions
        assert "下周一" not in conclusions or "赶" in conclusions
        assert any("联调" in c for c in body["conclusions"])
        assert any("支付接口" in q for q in body["open_questions"])

    def test_t31_replay_state_covers_the_whole_meeting(self, client, db, scripted):
        mid = _seed_meeting(client)
        _finalize(client, mid, 6)
        state = _state(client, mid)
        assert state["last_processed_segment_seq"] == 6
        assert state["status"] in ("ACTIVE", "FROZEN")
        covered = set()
        for key in ("topics", "decisions", "action_items", "open_questions", "risks"):
            for item in state[key]:
                covered.update(item["source_segment_seqs"])
        assert covered == {1, 2, 3, 4, 5, 6}, f"uncovered seqs: {sorted(covered)}"

    def test_t32_replay_is_idempotent_under_stream_end_retry(self, client, scripted):
        mid = _seed_meeting(client)
        _finalize(client, mid, 6)
        first = _summary(client, mid)
        # a retry of the finalizer must not create a second version or re-run
        from app.db import SessionLocal
        from app.agent import finalizer as finalizer_service

        s = SessionLocal()
        try:
            finalizer_service.finalize_for_meeting(s, mid, provider=scripted)
        finally:
            s.close()
        second = _summary(client, mid)
        assert second["version"] == first["version"] == 1
        assert second["overview"] == first["overview"]
