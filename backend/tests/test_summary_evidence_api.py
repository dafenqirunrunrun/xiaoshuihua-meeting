"""UI-INTEL-2B test suite — Final Summary evidence on the PUBLIC read path.

Step UI-INTEL-2B exposes evidence that already exists. It does NOT generate
evidence, does not repair citations, and does not change how a summary is
produced or persisted. So these tests are all about one question:

    given a persisted `meeting_summary` row, does the public Summary API hand
    the UI the right structured evidence — and nothing else?

Three properties carry the whole step, and each has a failure mode worth a test
of its own:

  1. **V2 is recognised, legacy is not.** The discrimination rests on
     `FinalMeetingSummary` setting `extra="forbid"`; a legacy payload carries
     `conclusions`, which V2 does not declare, so it FAILS validation. If that
     config were ever dropped, every legacy summary would silently advertise
     `decisions=[]` / `action_items=[]` and the UI would render an entry set
     that claims the summary was understood. T05/T12 pin this.
  2. **Degradation is None, never a 500.** A FAILED reason string, a truncated
     response, or a legacy row are all expected states. A read projection that
     raised would turn a perfectly readable summary into an error page. T07-T11.
  3. **Evidence is verbatim.** No dedupe, no sort, no dropping of impossible
     seqs. Normalizing belongs to the frontend (`utils/evidence.ts`); doing it
     here would create a second implementation and would hide a dangling
     citation instead of letting the UI degrade on it. T03/T04/T13.
"""

from __future__ import annotations

import json

import pytest

from app.agent.final_summary_schemas import (
    FinalMeetingSummary,
    project_summary_evidence,
    prune_code_fence,
)
from app.agent.summary_model import MeetingSummary
from app.agent.summary_service import STATUS_COMPLETED, STATUS_FAILED
from tests.test_final_summary_v2 import (  # noqa: F401  (fixture re-export)
    V2_RAW,
    _finalize,
    _seed_meeting,
    _summary,
    scripted,
)

# ---------------------------------------------------------------------------
# A legacy-shaped payload (MeetingSummarySchema). It has `conclusions`, which is
# exactly what makes it distinguishable from V2.
# ---------------------------------------------------------------------------
LEGACY_RAW = json.dumps(
    {
        "overview": "会议讨论了上线时间。",
        "key_topics": ["上线时间"],
        "conclusions": ["定于下周三上线"],
        "open_questions": ["是否灰度"],
    },
    ensure_ascii=False,
)

# The empty-meeting precomputed summary, byte-for-byte what
# `empty_meeting_summary()` produces.
EMPTY_MEETING_RAW = json.dumps(
    {
        "overview": "本次会议未产生有效转写内容。",
        "key_topics": [],
        "conclusions": [],
        "open_questions": [],
    },
    ensure_ascii=False,
)


def _put_raw(
    mid: str,
    raw: str | None,
    *,
    status: str = STATUS_COMPLETED,
    conclusions: list[str] | None = None,
) -> None:
    """Write a summary row directly — the read path is what is under test.

    Going through the DB (rather than driving a generation) is what makes the
    legacy / FAILED / malformed cases reachable at all: they are exactly the
    rows a real deployment accumulates and that no single generation run can
    produce on demand.

    `conclusions` must be set separately because the ORM stores the three lists
    as `*_json` columns with `@property` readers — writing only
    `raw_model_output` leaves the compatibility fields empty, which is NOT the
    shape a legacy row actually has.
    """
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        s.add(
            MeetingSummary(
                meeting_id=mid,
                version=1,
                status=status,
                overview="o",
                conclusions_json=MeetingSummary.dumps(conclusions) if conclusions else None,
                raw_model_output=raw,
            )
        )
        s.commit()
    finally:
        s.close()


class TestProjectionUnit:
    """The projection itself, without HTTP or DB in the way."""

    def test_t01_valid_v2_projects_all_four_kinds(self):
        ev = project_summary_evidence(V2_RAW)
        assert ev is not None
        assert [d.text for d in ev.decisions] == ["定于下周三上线"]
        assert ev.decisions[0].source_segment_seqs == [3]
        assert ev.action_items[0].task == "完成后端接口联调"
        assert ev.action_items[0].owner == "小王"
        assert ev.action_items[0].deadline == "周二之前"
        assert ev.action_items[0].source_segment_seqs == [4, 5]
        assert ev.risks[0].text == "支付接口稳定性需要确认"
        assert ev.risks[0].source_segment_seqs == [6]
        assert ev.open_questions == []

    def test_t02_open_questions_are_projected_with_evidence(self):
        raw = json.dumps(
            {
                "overview": "o",
                "key_topics": [],
                "decisions": [],
                "action_items": [],
                "open_questions": [{"text": "是否灰度？", "source_segment_seqs": [7, 9]}],
                "risks": [],
                "closing_summary": "",
            },
            ensure_ascii=False,
        )
        ev = project_summary_evidence(raw)
        assert ev is not None
        assert [q.text for q in ev.open_questions] == ["是否灰度？"]
        assert ev.open_questions[0].source_segment_seqs == [7, 9]

    def test_t03_null_owner_and_deadline_survive_as_null(self):
        """No default-substitution: 「待确认」 is a UI choice, not a data value.

        Filling a placeholder in here would make a genuinely-unknown owner
        indistinguishable from a model-guessed one at the API boundary, which
        is the opposite of what AGENT-2 §7 went to trouble to preserve.
        """
        raw = json.dumps(
            {
                "overview": "o",
                "key_topics": [],
                "decisions": [],
                "action_items": [
                    {"task": "整理清单", "owner": None, "deadline": None,
                     "source_segment_seqs": [6]}
                ],
                "open_questions": [],
                "risks": [],
                "closing_summary": "",
            },
            ensure_ascii=False,
        )
        ev = project_summary_evidence(raw)
        assert ev is not None
        assert ev.action_items[0].owner is None
        assert ev.action_items[0].deadline is None

    def test_t04_seqs_are_verbatim_not_normalized(self):
        """Duplicate / unsorted / impossible seqs pass through untouched.

        Normalization lives in `frontend/src/utils/evidence.ts` and nowhere
        else. A second implementation here would drift, and filtering seq 999
        would hide the dangling citation the UI is supposed to degrade on
        gracefully rather than have silently removed.
        """
        raw = json.dumps(
            {
                "overview": "o",
                "key_topics": [],
                "decisions": [{"text": "d", "source_segment_seqs": [5, 2, 5, 999, 2]}],
                "action_items": [],
                "open_questions": [],
                "risks": [],
                "closing_summary": "",
            },
            ensure_ascii=False,
        )
        ev = project_summary_evidence(raw)
        assert ev is not None
        assert ev.decisions[0].source_segment_seqs == [5, 2, 5, 999, 2]

    def test_t05_legacy_payload_is_rejected(self):
        """The core discrimination — `conclusions` is not a V2 field."""
        assert project_summary_evidence(LEGACY_RAW) is None

    def test_t06_fenced_v2_is_accepted_like_the_write_path(self):
        """A ```json fence must unwrap identically on read and write.

        A reader stricter than the writer would accept and persist a fenced
        response and then report `summary_evidence = null` forever — working in
        tests, broken in production against a real model.
        """
        fenced = "```json\n" + V2_RAW + "\n```"
        ev = project_summary_evidence(fenced)
        assert ev is not None
        assert [d.text for d in ev.decisions] == ["定于下周三上线"]
        # and the shared helper is the same one both sides call
        assert prune_code_fence(fenced).strip() == V2_RAW

    def test_t07_malformed_and_non_object_payloads_yield_none(self):
        for raw in (
            '{"overview":',
            "not json at all",
            "[1, 2, 3]",
            '"a bare string"',
            "null",
            "",
        ):
            assert project_summary_evidence(raw) is None, raw

    def test_t08_failed_reason_and_none_yield_none(self):
        assert project_summary_evidence("[summary failed] provider timeout") is None
        assert project_summary_evidence(None) is None

    def test_t09_empty_meeting_precomputed_yields_none(self):
        """A summary with no load-bearing items reports None, not all-empty.

        The empty-meeting summary is a LEGACY payload, so it cannot be told
        apart from one — and the honest answer for it is "nothing to trace to".
        An all-empty `SummaryEvidence` stays reachable via a real V2 meeting
        that genuinely decided nothing (T10), and the two are distinguished by
        the payload rather than by the response being null.
        """
        assert project_summary_evidence(EMPTY_MEETING_RAW) is None

    def test_t10_valid_v2_with_no_items_is_all_empty_not_none(self):
        raw = json.dumps(
            {
                "overview": "开了一次没有决议的会。",
                "key_topics": [],
                "decisions": [],
                "action_items": [],
                "open_questions": [],
                "risks": [],
                "closing_summary": "",
            },
            ensure_ascii=False,
        )
        ev = project_summary_evidence(raw)
        assert ev is not None
        assert ev.decisions == []
        assert ev.action_items == []
        assert ev.risks == []
        assert ev.open_questions == []

    def test_t11_v2_forbids_the_legacy_field_this_discrimination_relies_on(self):
        """If `extra="forbid"` is ever removed, T05 breaks — this says why."""
        with pytest.raises(Exception):
            FinalMeetingSummary.model_validate(json.loads(LEGACY_RAW))
        assert FinalMeetingSummary.model_config.get("extra") == "forbid"


class TestPublicResponse:
    """The projection as it appears in GET /summary, over a real DB row."""

    def _row_for(
        self,
        mid: str,
        raw: str | None,
        *,
        status: str = STATUS_COMPLETED,
        conclusions: list[str] | None = None,
    ) -> None:
        _put_raw(mid, raw, status=status, conclusions=conclusions)

    def test_t12_v2_row_exposes_structured_evidence(self, client):
        mid = client.post("/api/meetings", json={"title": "v2"}).json()["id"]
        self._row_for(mid, V2_RAW)
        body = _summary(client, mid)
        ev = body["summary_evidence"]
        assert ev is not None
        assert [d["text"] for d in ev["decisions"]] == ["定于下周三上线"]
        assert ev["action_items"][0]["source_segment_seqs"] == [4, 5]
        assert ev["risks"][0]["source_segment_seqs"] == [6]

    def test_t13_legacy_row_exposes_null_evidence(self, client):
        mid = client.post("/api/meetings", json={"title": "legacy"}).json()["id"]
        self._row_for(mid, LEGACY_RAW, conclusions=["定于下周三上线"])
        body = _summary(client, mid)
        assert body["summary_evidence"] is None
        # ...while the legacy compatibility fields are untouched
        assert body["conclusions"] == ["定于下周三上线"]

    def test_t14_failed_row_is_200_with_null_evidence(self, client):
        mid = client.post("/api/meetings", json={"title": "failed"}).json()["id"]
        self._row_for(mid, "[summary failed] provider timeout", status=STATUS_FAILED)
        body = _summary(client, mid)
        assert body["status"] == STATUS_FAILED
        assert body["summary_evidence"] is None

    def test_t15_malformed_raw_is_200_with_null_evidence_not_500(self, client):
        mid = client.post("/api/meetings", json={"title": "broken"}).json()["id"]
        self._row_for(mid, '{"overview": "truncated')
        body = _summary(client, mid)
        assert body["status"] == STATUS_COMPLETED
        assert body["summary_evidence"] is None

    def test_t16_no_row_still_404s(self, client):
        mid = client.post("/api/meetings", json={"title": "none"}).json()["id"]
        r = client.get(f"/api/meetings/{mid}/summary")
        assert r.status_code == 404

    def test_t17_existing_compatibility_fields_are_unchanged(self, client):
        """The step ADDS a field; it must not move any existing one."""
        mid = client.post("/api/meetings", json={"title": "compat"}).json()["id"]
        self._row_for(mid, V2_RAW)
        body = _summary(client, mid)
        assert set(body) == {
            "id", "meeting_id", "version", "status", "overview", "key_topics",
            "conclusions", "open_questions", "model_name", "raw_model_output",
            "created_at", "summary_evidence",
        }
        # raw_model_output keeps its existing behaviour (still exposed as-is)
        assert body["raw_model_output"] == V2_RAW

    def test_t18_projection_never_leaks_internal_detail(self, client):
        mid = client.post("/api/meetings", json={"title": "leak"}).json()["id"]
        self._row_for(mid, V2_RAW)
        blob = json.dumps(_summary(client, mid), ensure_ascii=False)
        for forbidden in ("Traceback", "api_key", "API_KEY", "Authorization", "prompt"):
            assert forbidden not in blob


class TestV2EndToEnd:
    """The whole chain, so the projection is proven against a REAL V2 row."""

    def test_t19_scripted_v2_meeting_exposes_traceable_evidence(self, client, scripted):
        """finish -> flush -> STREAM_END -> Finalizer -> V2 -> evidence.

        This is the case the unit tests cannot fully cover: the seqs here were
        produced by the AGENT-1 state, carried through the summary prompt, and
        returned by the model — not written into a fixture by hand. It is also
        the regression guard for the shared fence-pruning helper: if the read
        path stopped unwrapping what the write path accepts, this goes red.
        """
        mid = _seed_meeting(client)
        _finalize(client, mid, 6)
        body = _summary(client, mid)
        assert body["status"] == STATUS_COMPLETED

        ev = body["summary_evidence"]
        assert ev is not None, "a real V2 generation must expose structured evidence"
        assert [d["text"] for d in ev["decisions"]] == ["定于下周三上线"]
        assert ev["decisions"][0]["source_segment_seqs"] == [3]
        assert ev["action_items"][0]["owner"] == "小王"
        assert ev["action_items"][0]["deadline"] == "周二之前"
        assert ev["action_items"][0]["source_segment_seqs"] == [4, 5]
        assert ev["risks"][0]["source_segment_seqs"] == [6]

    def test_t20_empty_meeting_exposes_null_evidence(self, client, scripted):
        mid = client.post("/api/meetings", json={"title": "empty"}).json()["id"]
        client.post(f"/api/meetings/{mid}/start")
        _finalize(client, mid, 0)
        body = _summary(client, mid)
        assert body["status"] == STATUS_COMPLETED
        assert body["summary_evidence"] is None
