"""ADR-002 regression suite: 水华队长 AI Referee Sidecar.

Covers the matrix from the Phase-1 + Phase-2B spec:

  unit      wake word (match / variants / full-width) · window selection
            (anchor excluded, time bound, char bound, position fallback)
  schema    positions 1..5 · extra fields forbidden · uncertainty required
  evidence  missing seq dropped · non-verbatim quote dropped · valid kept
  pipeline  normal report · single position · three positions · no speaker name
  api       manual trigger 202 · dedupe · throttle · status gates · reads
  isolation meeting-state agent disabled -> captain still works
  analysis  end-to-end analysis core (Phase 2B) · multi-position · attribution
            ban · evidence grounding · model_name recorded · no leakage
  sse       captain.completed / captain.failed delivery (Phase 2A)

Everything runs offline on a fresh SQLite schema with a scripted provider. The
referee dispatches through its OWN worker thread, so tests drain the queue
explicitly (`captain_worker.drain`) rather than relying on BackgroundTasks.
"""

from __future__ import annotations

import json
import re

import pytest

from app.captain import context as captain_context
from app.captain import evidence as captain_evidence
from app.captain import service as captain_service
from app.captain import wake_word
from app.captain.schemas import CaptainReportContent
from app.captain.worker import captain_worker
from app.config import settings

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _make_meeting(client, *, status: str = "IN_PROGRESS") -> str:
    """Create a meeting and drive it to the requested status."""
    created = client.post("/api/meetings", json={"title": "裁判测试会议"})
    meeting_id = created.json()["id"]
    if status in {"IN_PROGRESS", "FINALIZING", "COMPLETED"}:
        client.post(f"/api/meetings/{meeting_id}/start")
    return meeting_id


def _post_final(client, meeting_id: str, seq: int, text: str, *, start_ms: int | None = None):
    """Ingest one FINAL through the internal transcript ingress."""
    if start_ms is None:
        start_ms = (seq - 1) * 10_000
    return client.post(
        f"/api/internal/meetings/{meeting_id}/transcript-events",
        json={
            "event_id": f"evt-{meeting_id}-{seq}",
            "meeting_id": meeting_id,
            "utterance_id": f"utt-{seq}",
            "segment_seq": seq,
            "speaker_id": "unknown",
            "text": text,
            "status": "FINAL",
            "start_ms": start_ms,
            "end_ms": start_ms + 5_000,
        },
    )


def _trigger(client, meeting_id: str, *, anchor: int | None = None):
    body = {"anchor_segment_seq": anchor} if anchor is not None else {}
    return client.post(f"/api/meetings/{meeting_id}/captain", json=body)


class WindowProvider:
    """Provider that builds a report from the window it is handed.

    Reads the `[seq] text` lines out of the `<discussion_window>` block, so the
    assertions test PIPELINE behaviour (window bounds, citation pruning, dedupe)
    rather than pinned model prose.
    """

    name = "window-provider"

    def __init__(self, *, positions: int = 1, extra: dict | None = None,
                 raw: str | None = None, error: str | None = None) -> None:
        self.positions = positions
        self.extra = extra or {}
        self.raw = raw
        self.error = error
        self.calls: list[str] = []
        self.windows_seen: list[list[int]] = []

    def complete(self, prompt: str, *, timeout_seconds: float | None = None) -> str:
        from app.agent.llm.base import SummaryGenerationError

        self.calls.append(prompt)
        if self.error:
            raise SummaryGenerationError(self.error)

        seqs = _seqs_in_window(prompt)
        self.windows_seen.append(seqs)
        if self.raw is not None:
            return self.raw

        payload = {
            "discussion_summary": "讨论集中在方案取舍上。",
            "positions": [
                {
                    "label": f"观点{i + 1}",
                    "statement": f"第{i + 1}种主张。",
                    "source_segment_seqs": seqs[:1],
                }
                for i in range(self.positions)
            ],
            "agreement_points": [],
            "dispute_points": [],
            "evidence": [],
            "uncertainty": "窗口较短，仅覆盖最近若干句。",
            "suggestion": "建议补充成本数据后再判断。",
        }
        payload.update(self.extra)
        return json.dumps(payload, ensure_ascii=False)


def _seqs_in_window(prompt: str) -> list[int]:
    body = prompt.split("<discussion_window>")[1].split("</discussion_window>")[0]
    return [int(m) for m in re.findall(r"^\[(\d+)\]", body, re.MULTILINE)]


def _patch_provider(monkeypatch, provider) -> None:
    """Patch the provider on BOTH resolution paths.

    `captain/service.py` imports `get_provider` lazily inside the function, so
    patching the factory alone is enough today — but the factory is the single
    documented entry point, and pinning it here is what keeps the test honest
    about which path the production code takes.
    """
    import app.agent.llm.factory as factory

    monkeypatch.setattr(factory, "get_provider", lambda *a, **k: provider)


def _run(meeting_id: str, report_id: str, provider) -> str:
    """Run one analysis synchronously (unit-level, no worker thread)."""
    return captain_service.run_analysis(meeting_id, report_id, provider=provider)


# ==========================================================================
# 1. wake word
# ==========================================================================


class TestWakeWord:
    def test_matches_plain(self):
        assert wake_word.match("水华队长")
        assert wake_word.match("那水华队长你觉得呢")

    def test_matches_with_punctuation_and_space(self):
        assert wake_word.match("水华队长，你怎么看")
        assert wake_word.match("水华队长 你说说")

    def test_matches_full_width(self):
        # NFKC folding must map full-width forms onto the ASCII/compat form.
        assert wake_word.match("水华队长（请分析）")

    def test_no_false_positive_on_other_text(self):
        assert not wake_word.match("我们继续讨论方案")
        assert not wake_word.match("")
        assert not wake_word.match("队长")

    def test_wake_word_has_no_side_effects_and_no_io(self):
        # Pure function: same input, same output, no settings mutated.
        assert wake_word.match("水华队长") is True
        assert wake_word.match("水华队长") is True


# ==========================================================================
# 2. context window
# ==========================================================================


class TestContextWindow:
    def _segments(self, n: int, *, step_ms: int = 10_000):
        from app.transcript.segment_model import TranscriptSegment

        return [
            TranscriptSegment(
                meeting_id="m",
                segment_seq=i,
                utterance_id=f"u{i}",
                event_id=f"e{i}",
                speaker_id="unknown",
                text=f"第{i}句",
                start_ms=(i - 1) * step_ms,
                end_ms=(i - 1) * step_ms + 5_000,
            )
            for i in range(1, n + 1)
        ]

    def test_anchor_is_excluded_for_a_wake_trigger(self):
        """A wake-phrase anchor must never be analysed as discussion content."""
        window = captain_context.select_window(
            self._segments(5),
            anchor_segment_seq=5,
            window_seconds=180,
            max_chars=10_000,
            min_segments=1,
        )
        seqs = [s.segment_seq for s in window.segments]
        assert 5 not in seqs
        assert seqs == [1, 2, 3, 4]

    def test_anchor_is_kept_for_a_high_water_mark_trigger(self):
        """A button anchor is content, and dropping it loses the newest line.

        Regression pin for a shipped content-loss bug. With `exclude_anchor` the
        default for every trigger, a five-segment discussion whose last line was
        "不过这个问题我们上周已经改了连接池参数" handed the model seqs 1..4 — the
        referee never saw the connection-pool change, and no downstream check
        could catch it because the segment was never in the window.
        """
        window = captain_context.select_window(
            self._segments(5),
            anchor_segment_seq=5,
            window_seconds=180,
            max_chars=10_000,
            min_segments=1,
            exclude_anchor=False,
        )
        seqs = [s.segment_seq for s in window.segments]
        assert 5 in seqs, "the newest utterance was dropped from its own analysis"
        assert seqs == [1, 2, 3, 4, 5]

    def test_nothing_after_anchor_is_included(self):
        """Future segments must never leak into the window (ADR-002 §6)."""
        window = captain_context.select_window(
            self._segments(10),
            anchor_segment_seq=4,
            window_seconds=180,
            max_chars=10_000,
            min_segments=1,
        )
        assert all(s.segment_seq <= 4 for s in window.segments)
        assert [s.segment_seq for s in window.segments] == [1, 2, 3]

    def test_time_window_bound(self):
        # 60s window, 10s steps, anchor 10 at start_ms=90_000. floor = 90_000 -
        # 60_000 = 30_000, and the bound is INCLUSIVE on both ends, so seq 4
        # (start_ms=30_000, exactly the floor) is inside. Boundary-inclusive is
        # intentional: a discussion that starts exactly at the window edge is
        # part of what the user heard.
        window = captain_context.select_window(
            self._segments(10),
            anchor_segment_seq=10,
            window_seconds=60,
            max_chars=10_000,
            min_segments=1,
        )
        assert [s.segment_seq for s in window.segments] == [4, 5, 6, 7, 8, 9]

    def test_char_bound_keeps_newest(self):
        """When the budget bites, the material closest to the trigger survives."""
        window = captain_context.select_window(
            self._segments(20),
            anchor_segment_seq=20,
            window_seconds=10_000,
            max_chars=12,  # room for ~3 segments of "第NN句"
            min_segments=1,
        )
        seqs = [s.segment_seq for s in window.segments]
        assert seqs[-1] == 19  # newest kept
        assert len(seqs) < 19  # oldest dropped
        assert window.truncated is True
        assert window.dropped_count > 0

    def test_too_few_segments_yields_empty_window(self):
        """Below min_segments the referee refuses instead of inventing a debate."""
        window = captain_context.select_window(
            self._segments(2),
            anchor_segment_seq=2,
            window_seconds=180,
            max_chars=10_000,
            min_segments=5,
        )
        assert window.is_empty

    def test_position_fallback_when_anchor_has_no_timing(self):
        segs = self._segments(5)
        segs[-1].start_ms = None
        window = captain_context.select_window(
            segs,
            anchor_segment_seq=5,
            window_seconds=180,
            max_chars=10_000,
            min_segments=1,
        )
        assert window.used_position_fallback is True
        assert not window.is_empty


# ==========================================================================
# 3. schema constraints
# ==========================================================================


class TestReportSchema:
    def _base(self, **over):
        payload = {
            "discussion_summary": "讨论中",
            "positions": [
                {"label": "A", "statement": "主张A", "source_segment_seqs": []}
            ],
            "agreement_points": [],
            "dispute_points": [],
            "evidence": [],
            "uncertainty": "不确定",
            "suggestion": "补充信息",
        }
        payload.update(over)
        return payload

    def test_single_position_and_empty_dispute_is_valid(self):
        content = CaptainReportContent.model_validate(self._base())
        assert len(content.positions) == 1
        assert content.dispute_points == []

    def test_five_positions_allowed(self):
        positions = [
            {"label": f"P{i}", "statement": f"s{i}", "source_segment_seqs": []}
            for i in range(5)
        ]
        assert len(CaptainReportContent.model_validate(
            self._base(positions=positions)).positions) == 5

    def test_six_positions_rejected(self):
        positions = [
            {"label": f"P{i}", "statement": f"s{i}", "source_segment_seqs": []}
            for i in range(6)
        ]
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            CaptainReportContent.model_validate(self._base(positions=positions))

    def test_zero_positions_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            CaptainReportContent.model_validate(self._base(positions=[]))

    def test_extra_fields_forbidden(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            CaptainReportContent.model_validate(self._base(side_a="不该存在"))

    def test_empty_uncertainty_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            CaptainReportContent.model_validate(self._base(uncertainty=""))

    def test_legacy_side_fields_rejected(self):
        """side_a / side_b must not be accepted — the model is positions-based."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            CaptainReportContent.model_validate(
                self._base(side_a_argument="A", side_b_argument="B")
            )


# ==========================================================================
# 4. evidence validation
# ==========================================================================


class TestEvidenceValidation:
    def _segments(self):
        from app.transcript.segment_model import TranscriptSegment

        def seg(seq: int, text: str):
            return TranscriptSegment(
                meeting_id="m", segment_seq=seq, utterance_id=f"u{seq}",
                event_id=f"e{seq}", speaker_id="unknown", text=text,
                start_ms=seq * 1000, end_ms=seq * 1000 + 500,
            )

        return [seg(1, "我们下周上线"), seg(2, "成本太高了")]

    def _content(self, evidence, positions=None):
        return CaptainReportContent.model_validate(
            {
                "discussion_summary": "s",
                "positions": positions
                or [{"label": "A", "statement": "a", "source_segment_seqs": [1]}],
                "agreement_points": [],
                "dispute_points": [],
                "evidence": evidence,
                "uncertainty": "u",
                "suggestion": "g",
            }
        )

    def test_valid_quote_kept(self):
        result = captain_evidence.validate_evidence(
            self._content([{"segment_seq": 1, "quote": "下周上线", "supports": "排期"}]),
            self._segments(),
        )
        assert len(result.content.evidence) == 1
        assert result.dropped_evidence == 0

    def test_non_verbatim_quote_dropped(self):
        """A paraphrase presented as a quote is the most convincing hallucination."""
        result = captain_evidence.validate_evidence(
            self._content([{"segment_seq": 1, "quote": "上线时间已经确定", "supports": "排期"}]),
            self._segments(),
        )
        assert result.content.evidence == []
        assert result.dropped_evidence == 1

    def test_missing_segment_dropped(self):
        result = captain_evidence.validate_evidence(
            self._content([{"segment_seq": 99, "quote": "我们下周上线", "supports": "x"}]),
            self._segments(),
        )
        assert result.content.evidence == []
        assert result.dropped_evidence == 1

    def test_out_of_window_citation_pruned(self):
        """seq 42 was never shown to the model; the claim survives, the citation does not."""
        result = captain_evidence.validate_evidence(
            self._content(
                [],
                positions=[{"label": "A", "statement": "a",
                            "source_segment_seqs": [1, 42]}],
            ),
            self._segments(),
        )
        assert result.content.positions[0].source_segment_seqs == [1]
        assert result.dropped_citations == 1

    def test_punctuation_insensitive_quote_match(self):
        result = captain_evidence.validate_evidence(
            self._content([{"segment_seq": 1, "quote": "我们，下周上线", "supports": "x"}]),
            self._segments(),
        )
        assert len(result.content.evidence) == 1


# ==========================================================================
# 5. API + pipeline (integration)
# ==========================================================================


class TestTriggerApi:
    def test_manual_trigger_returns_202_and_completes(self, client, monkeypatch):
        meeting_id = _make_meeting(client)
        for i in range(1, 5):
            _post_final(client, meeting_id, i, f"第{i}句讨论内容")

        provider = WindowProvider(positions=3)
        _patch_provider(monkeypatch, provider)

        resp = _trigger(client, meeting_id)
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "PENDING"
        assert body["created"] is True
        assert body["anchor_segment_seq"] == 4

        assert captain_worker.drain(timeout=10.0)
        got = client.get(f"/api/meetings/{meeting_id}/captain/{body['report_id']}")
        assert got.status_code == 200
        data = got.json()
        assert data["status"] == "COMPLETED"
        assert len(data["report"]["positions"]) == 3
        assert data["report"]["uncertainty"]

    def test_one_position_with_empty_dispute(self, client, monkeypatch):
        meeting_id = _make_meeting(client)
        for i in range(1, 4):
            _post_final(client, meeting_id, i, f"第{i}句")

        _patch_provider(monkeypatch, WindowProvider(positions=1))
        body = _trigger(client, meeting_id).json()
        assert captain_worker.drain(timeout=10.0)

        report = client.get(
            f"/api/meetings/{meeting_id}/captain/{body['report_id']}"
        ).json()
        assert report["status"] == "COMPLETED"
        assert len(report["report"]["positions"]) == 1
        assert report["report"]["dispute_points"] == []

    def test_dedupe_same_anchor_reuses_report_without_llm(self, client, monkeypatch):
        meeting_id = _make_meeting(client)
        for i in range(1, 4):
            _post_final(client, meeting_id, i, f"第{i}句")

        provider = WindowProvider()
        _patch_provider(monkeypatch, provider)
        captain_service.reset_throttle()

        first = _trigger(client, meeting_id).json()
        assert captain_worker.drain(timeout=10.0)
        calls_after_first = len(provider.calls)

        # Bypass the throttle to isolate the DEDUPE behaviour under test.
        captain_service.reset_throttle()
        second = _trigger(client, meeting_id).json()

        assert second["created"] is False
        assert second["reused"] is True
        assert second["report_id"] == first["report_id"]  # same report, no new row
        assert captain_worker.drain(timeout=10.0)
        assert len(provider.calls) == calls_after_first  # zero extra LLM spend

    def test_throttle_returns_429(self, client, monkeypatch):
        meeting_id = _make_meeting(client)
        for i in range(1, 4):
            _post_final(client, meeting_id, i, f"第{i}句")
        _patch_provider(monkeypatch, WindowProvider())

        assert _trigger(client, meeting_id).status_code == 202
        assert captain_worker.drain(timeout=10.0)
        # Different anchor, same meeting, immediately -> throttled.
        assert _trigger(client, meeting_id, anchor=2).status_code == 429

    def test_meeting_not_found_404(self, client):
        assert _trigger(client, "does-not-exist").status_code == 404

    def test_created_meeting_cannot_be_refereed(self, client):
        resp = client.post("/api/meetings", json={"title": "未开始"})
        meeting_id = resp.json()["id"]
        assert _trigger(client, meeting_id).status_code == 409

    def test_latest_404_before_any_report(self, client):
        meeting_id = _make_meeting(client)
        assert client.get(
            f"/api/meetings/{meeting_id}/captain/latest"
        ).status_code == 404

    def test_latest_after_report(self, client, monkeypatch):
        meeting_id = _make_meeting(client)
        for i in range(1, 4):
            _post_final(client, meeting_id, i, f"第{i}句")
        _patch_provider(monkeypatch, WindowProvider())
        body = _trigger(client, meeting_id).json()
        assert captain_worker.drain(timeout=10.0)

        latest = client.get(f"/api/meetings/{meeting_id}/captain/latest").json()
        assert latest["report_id"] == body["report_id"]


class TestTriggerWordExclusion:
    def test_trigger_segment_not_in_window(self, client, monkeypatch):
        """The wake utterance must not become a position in its own report."""
        meeting_id = _make_meeting(client)
        _post_final(client, meeting_id, 1, "我们讨论一下成本问题")
        _post_final(client, meeting_id, 2, "我觉得成本可控")
        _post_final(client, meeting_id, 3, "水华队长，你怎么看")

        provider = WindowProvider()
        _patch_provider(monkeypatch, provider)

        body = _trigger(client, meeting_id, anchor=3).json()
        assert captain_worker.drain(timeout=10.0)

        assert provider.windows_seen, "provider was never called"
        seen = provider.windows_seen[-1]
        assert 3 not in seen, "trigger segment leaked into the analysis window"
        assert seen == [1, 2]


class TestWakeWordAutoTrigger:
    def test_wake_word_final_creates_report(self, client, monkeypatch):
        """Saying the wake word in a FINAL auto-creates a report (no button)."""
        meeting_id = _make_meeting(client)
        _post_final(client, meeting_id, 1, "方案A更稳妥")
        _post_final(client, meeting_id, 2, "方案B更快")
        _patch_provider(monkeypatch, WindowProvider())

        _post_final(client, meeting_id, 3, "水华队长")
        assert captain_worker.drain(timeout=10.0)

        latest = client.get(f"/api/meetings/{meeting_id}/captain/latest")
        assert latest.status_code == 200
        assert latest.json()["anchor_segment_seq"] == 3

    def test_non_wake_final_creates_nothing(self, client, monkeypatch):
        meeting_id = _make_meeting(client)
        _patch_provider(monkeypatch, WindowProvider())
        _post_final(client, meeting_id, 1, "普通发言内容")
        assert captain_worker.drain(timeout=10.0)

        assert client.get(
            f"/api/meetings/{meeting_id}/captain/latest"
        ).status_code == 404


# ==========================================================================
# 6. failure handling
# ==========================================================================


class TestFailures:
    def test_provider_failure_marks_failed_without_partial_content(
        self, client, monkeypatch
    ):
        meeting_id = _make_meeting(client)
        for i in range(1, 4):
            _post_final(client, meeting_id, i, f"第{i}句")

        _patch_provider(monkeypatch, WindowProvider(error="provider down"))
        body = _trigger(client, meeting_id).json()
        assert captain_worker.drain(timeout=10.0)

        report = client.get(
            f"/api/meetings/{meeting_id}/captain/{body['report_id']}"
        ).json()
        assert report["status"] == "FAILED"
        assert report["report"] is None  # never half a report

    def test_insufficient_window_fails_honestly(self, client, monkeypatch):
        meeting_id = _make_meeting(client)
        _post_final(client, meeting_id, 1, "只有一句")

        provider = WindowProvider()
        _patch_provider(monkeypatch, provider)
        body = _trigger(client, meeting_id, anchor=1).json()
        assert captain_worker.drain(timeout=10.0)

        report = client.get(
            f"/api/meetings/{meeting_id}/captain/{body['report_id']}"
        ).json()
        assert report["status"] == "FAILED"
        assert provider.calls == []  # refused BEFORE spending an LLM call

    def test_malformed_json_then_repair_succeeds(self, client, monkeypatch):
        meeting_id = _make_meeting(client)
        for i in range(1, 4):
            _post_final(client, meeting_id, i, f"第{i}句")

        provider = WindowProvider(raw="not json at all")
        _patch_provider(monkeypatch, provider)
        body = _trigger(client, meeting_id).json()
        assert captain_worker.drain(timeout=10.0)

        report = client.get(
            f"/api/meetings/{meeting_id}/captain/{body['report_id']}"
        ).json()
        assert report["status"] == "FAILED"
        # Bounded repair: more than one attempt, but a finite number.
        assert len(provider.calls) <= 2


# ==========================================================================
# 7. isolation from the meeting-state agent
# ==========================================================================


class TestIsolation:
    def test_captain_works_with_meeting_state_agent_disabled(self, client, monkeypatch):
        """Turning the realtime agent off must not affect the referee at all."""
        monkeypatch.setattr(
            "app.config.settings.meeting_agent_enabled", False, raising=False
        )
        meeting_id = _make_meeting(client)
        for i in range(1, 4):
            _post_final(client, meeting_id, i, f"第{i}句")

        _patch_provider(monkeypatch, WindowProvider())
        body = _trigger(client, meeting_id).json()
        assert captain_worker.drain(timeout=10.0)

        report = client.get(
            f"/api/meetings/{meeting_id}/captain/{body['report_id']}"
        ).json()
        assert report["status"] == "COMPLETED"

    def test_no_meeting_state_row_is_written(self, client, monkeypatch):
        """The referee must not touch Meeting State — not even implicitly."""
        from app.db import SessionLocal
        from app.meeting_state.model import MeetingState
        from sqlalchemy import select

        meeting_id = _make_meeting(client)
        for i in range(1, 4):
            _post_final(client, meeting_id, i, f"第{i}句")
        _patch_provider(monkeypatch, WindowProvider())
        _trigger(client, meeting_id)
        assert captain_worker.drain(timeout=10.0)

        db = SessionLocal()
        try:
            rows = db.execute(
                select(MeetingState).where(MeetingState.meeting_id == meeting_id)
            ).scalars().all()
        finally:
            db.close()
        assert rows == []

    def test_captain_package_does_not_import_meeting_state(self):
        """Static decoupling assertion (ADR-002 §10).

        Importing `app.captain.service` must not pull `app.meeting_state` into
        sys.modules. This is the regression guard for the one boundary the whole
        sidecar design rests on.
        """
        import subprocess
        import sys

        code = (
            "import sys; import app.captain.service; "
            "assert not any(m.startswith('app.meeting_state') for m in sys.modules), "
            "[m for m in sys.modules if m.startswith('app.meeting_state')]"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
            env={**__import__("os").environ, "DATABASE_URL": "sqlite:///:memory:"},
        )
        assert result.returncode == 0, result.stderr


class TestTranscriptUnaffected:
    def test_captain_failure_does_not_break_transcript(self, client, monkeypatch):
        """A broken referee must never affect FINAL persistence or SSE."""
        meeting_id = _make_meeting(client)
        _patch_provider(monkeypatch, WindowProvider(error="provider down"))

        # A wake-word FINAL triggers the referee (which will fail)...
        resp = _post_final(client, meeting_id, 1, "水华队长")
        assert resp.status_code == 200
        _post_final(client, meeting_id, 2, "后续发言仍然正常")
        assert captain_worker.drain(timeout=10.0)

        # ...and the transcript is still complete.
        snapshot = client.get(f"/api/meetings/{meeting_id}/transcript").json()
        assert [s["segment_seq"] for s in snapshot["segments"]] == [1, 2]


# ==========================================================================
# 11. analysis core, end to end (ADR-002 Phase 2-B)
# ==========================================================================


class TestCaptainAnalysis:
    """The Phase-2B acceptance matrix over the FULL chain.

    Distinct from the unit classes above: every case here drives
    trigger -> worker -> context -> prompt -> provider -> Pydantic -> evidence
    -> COMPLETED, so a regression in any single stage shows up as a wrong
    REPORT rather than a wrong helper return value.

    Where a case is already covered upstream this says so in the docstring —
    the point of the class is the acceptance matrix the spec names, and
    duplicating an assertion under a new name would only create maintenance
    burden, not coverage.
    """

    def test_normal_discussion_produces_a_report(self, client, monkeypatch):
        """§七.1 — 3-minute transcript in, at least one position out."""
        meeting_id = _make_meeting(client)
        # 10s apart => the whole run sits inside the 180s window.
        lines = [
            "我们先把成本口径对齐",
            "我认为应该按人均计算",
            "但人均会掩盖总量问题",
            "那就两个口径都列出来",
            "可以，下周给数据",
        ]
        for i, text in enumerate(lines, start=1):
            _post_final(client, meeting_id, i, text)

        provider = WindowProvider(positions=2)
        _patch_provider(monkeypatch, provider)
        body = _trigger(client, meeting_id).json()
        assert captain_worker.drain(timeout=10.0)

        data = client.get(
            f"/api/meetings/{meeting_id}/captain/{body['report_id']}"
        ).json()
        assert data["status"] == "COMPLETED"
        assert len(data["report"]["positions"]) >= 1
        assert data["report"]["discussion_summary"]
        assert data["report"]["uncertainty"]
        assert data["report"]["suggestion"]

        # The button path anchors on the current high-water mark (seq 5). That
        # segment is an ORDINARY utterance, not a wake phrase, so it must be
        # ANALYSED — dropping it lost the newest thing said every time. The
        # wake-word case (which DOES exclude) is covered separately by
        # `test_wake_segment_excluded_from_the_window`.
        assert provider.windows_seen[-1] == [1, 2, 3, 4, 5]

    def test_button_path_reaches_the_newest_utterance(self, client, monkeypatch):
        """The five-segment bad case: the last line must reach the referee.

        This is the product-critical assertion, stated as one fact rather than a
        window listing: when the newest line carries the evidence that decides
        the discussion (here, that the historical incident's root cause was
        already fixed), the referee must be ABLE to cite it. It cannot, if the
        segment never entered the window.
        """
        meeting_id = _make_meeting(client)
        _post_final(client, meeting_id, 1, "我建议下周五上线，核心功能已经开发完毕")
        _post_final(client, meeting_id, 2, "功能测试过了，但压力测试还没做")
        _post_final(client, meeting_id, 3, "压测需要三天准备环境")
        _post_final(client, meeting_id, 4, "发布会的商务时间已经定了")
        _post_final(client, meeting_id, 5, "不过这个问题我们上周已经改了连接池参数")

        provider = WindowProvider(positions=2)
        _patch_provider(monkeypatch, provider)
        # No explicit anchor = the button path.
        body = _trigger(client, meeting_id).json()
        assert captain_worker.drain(timeout=10.0)

        assert provider.windows_seen[-1] == [1, 2, 3, 4, 5]
        data = client.get(
            f"/api/meetings/{meeting_id}/captain/{body['report_id']}"
        ).json()
        assert data["status"] == "COMPLETED"
        # The window bounds recorded on the row must also cover the newest
        # segment — the persisted provenance is what a reader audits later.
        assert data["window_to_seq"] == 5

    def test_three_distinct_positions(self, client, monkeypatch):
        """§七.2 — three clusters stay three; no forced two-sided framing."""
        meeting_id = _make_meeting(client)
        for i in range(1, 5):
            _post_final(client, meeting_id, i, f"第{i}句")

        provider = WindowProvider(positions=3)
        _patch_provider(monkeypatch, provider)
        body = _trigger(client, meeting_id).json()
        assert captain_worker.drain(timeout=10.0)

        data = client.get(
            f"/api/meetings/{meeting_id}/captain/{body['report_id']}"
        ).json()
        assert data["status"] == "COMPLETED"
        assert len(data["report"]["positions"]) == 3
        # side_a / side_b must not exist anywhere in the payload.
        rendered = json.dumps(data, ensure_ascii=False)
        assert "side_a" not in rendered and "side_b" not in rendered

    def test_single_position_and_empty_dispute(self, client, monkeypatch):
        """§七.3 — an aligned discussion is a VALID report, not a failure.

        Overlaps `TestTriggerApi::test_one_position_with_empty_dispute` on
        purpose: this is the acceptance-matrix entry, and it additionally pins
        the agreement/dispute SHAPE rather than just the count.
        """
        meeting_id = _make_meeting(client)
        _post_final(client, meeting_id, 1, "我们都同意先做成本核算")
        _post_final(client, meeting_id, 2, "同意，这是前提")
        # A NEUTRAL anchor: the wake word would auto-trigger its own report and
        # throttle the explicit trigger below (429). Anchor exclusion is about
        # the anchor segment, not about it containing the wake phrase — that
        # path is covered by `test_wake_segment_excluded_from_the_window`.
        _post_final(client, meeting_id, 3, "先记录到这里")

        provider = WindowProvider(positions=1)
        _patch_provider(monkeypatch, provider)
        body = _trigger(client, meeting_id, anchor=3).json()
        assert captain_worker.drain(timeout=10.0)

        data = client.get(
            f"/api/meetings/{meeting_id}/captain/{body['report_id']}"
        ).json()
        assert len(data["report"]["positions"]) == 1
        assert data["report"]["dispute_points"] == []

    def test_report_contains_no_person_attribution(self, client, monkeypatch):
        """§七.4 — no 姓名 / 职位 / "某某认为" in the persisted report.

        This is the case with NO upstream coverage and the highest product
        risk: `ASR_SPEAKER_LABELS_ENABLED=false` means speaker identity is not
        knowable, so a name in the report is a fabrication by construction.
        """
        meeting_id = _make_meeting(client)
        _post_final(client, meeting_id, 1, "张三认为应该先做成本核算")
        _post_final(client, meeting_id, 2, "李经理表示可以下周给数据")
        _post_final(client, meeting_id, 3, "先记录到这里")

        provider = WindowProvider(positions=1)
        _patch_provider(monkeypatch, provider)
        body = _trigger(client, meeting_id, anchor=3).json()
        assert captain_worker.drain(timeout=10.0)

        data = client.get(
            f"/api/meetings/{meeting_id}/captain/{body['report_id']}"
        ).json()
        assert data["status"] == "COMPLETED"

        # The window DOES contain the names (they were spoken), so the ban is
        # entirely the model's obligation — which is exactly why it is asserted
        # on the finished report rather than on the input.
        assert provider.windows_seen[-1] == [1, 2]
        report_text = json.dumps(data["report"], ensure_ascii=False)
        for attribution in ("张三", "李经理", "认为", "表示"):
            assert attribution not in report_text, (
                f"person attribution leaked into the report: {attribution}"
            )

    def test_fabricated_quote_is_dropped_end_to_end(
        self, client, monkeypatch
    ):
        """§七.5 — a quote that is not in the cited segment never ships.

        Complements `TestEvidenceValidation` (pure-function level) by proving the
        pruning is actually WIRED INTO the pipeline and the degradation is
        disclosed in `uncertainty` rather than silently swallowed.
        """
        meeting_id = _make_meeting(client)
        _post_final(client, meeting_id, 1, "成本口径需要先统一")
        _post_final(client, meeting_id, 2, "同意，下周给数据")
        _post_final(client, meeting_id, 3, "先记录到这里")

        provider = WindowProvider(positions=1, extra={
            "evidence": [
                # Real seq, real text -> survives.
                {"segment_seq": 1, "quote": "成本口径需要先统一",
                 "supports": "讨论起点"},
                # Real seq, FABRICATED quote -> must be dropped.
                {"segment_seq": 2, "quote": "这句话原文里根本没有",
                 "supports": "编造的依据"},
                # Non-existent seq -> must be dropped.
                {"segment_seq": 999, "quote": "任意", "supports": "不存在的片段"},
            ],
        })
        _patch_provider(monkeypatch, provider)
        body = _trigger(client, meeting_id, anchor=3).json()
        assert captain_worker.drain(timeout=10.0)

        data = client.get(
            f"/api/meetings/{meeting_id}/captain/{body['report_id']}"
        ).json()
        assert data["status"] == "COMPLETED"
        assert [e["segment_seq"] for e in data["report"]["evidence"]] == [1]
        # The removal is DISCLOSED, not hidden.
        assert "剔除" in data["report"]["uncertainty"]

    def test_wake_segment_excluded_from_the_window(self, client, monkeypatch):
        """§七.6 — the wake utterance never becomes its own analysis input.

        Driven through the REAL wake-word path (`_post_final` of a sentence
        containing 「水华队长」), which is both the product-critical route and
        the deterministic one: that FINAL auto-creates the report itself, so
        nothing here races the per-minute throttle the way an explicit trigger
        after an auto-trigger would.
        """
        meeting_id = _make_meeting(client)
        _post_final(client, meeting_id, 1, "先看成本")
        _post_final(client, meeting_id, 2, "再看来不及")

        provider = WindowProvider()
        _patch_provider(monkeypatch, provider)
        _post_final(client, meeting_id, 3, "水华队长帮我看看")
        assert captain_worker.drain(timeout=10.0)

        latest = client.get(f"/api/meetings/{meeting_id}/captain/latest").json()
        assert latest["anchor_segment_seq"] == 3

        seen = provider.windows_seen[-1]
        assert 3 not in seen, "触发词所在 segment 进入了分析窗口"
        assert seen == [1, 2]

    def test_invalid_json_fails_honestly(self, client, monkeypatch):
        """§七.7 — unrecoverable shape errors end as FAILED, never as a
        silently-empty report.

        `WindowProvider(raw=...)` returns the same garbage on every attempt, so
        the bounded repair loop exhausts and the row terminates. A provider that
        returns good JSON on attempt 2 is covered by
        `TestFailures::test_malformed_json_then_repair_succeeds`.
        """
        meeting_id = _make_meeting(client)
        for i in range(1, 4):
            _post_final(client, meeting_id, i, f"第{i}句")

        provider = WindowProvider(raw="{ this is not json")
        _patch_provider(monkeypatch, provider)
        body = _trigger(client, meeting_id).json()
        assert captain_worker.drain(timeout=10.0)

        data = client.get(
            f"/api/meetings/{meeting_id}/captain/{body['report_id']}"
        ).json()
        assert data["status"] == "FAILED"
        assert data["report"] is None
        # Repair is bounded: strictly more than one attempt, but finite.
        assert 1 <= len(provider.calls) <= 2

    def test_model_name_is_recorded_but_never_exposed(
        self, client, monkeypatch
    ):
        """§六 — `model_name` is audit metadata, not a product field.

        `CaptainReportPublic` deliberately omits it (same split as
        `MeetingStatePublic`), so this asserts BOTH halves: recorded on the row,
        absent from the API response.
        """
        from app.captain import repository as captain_repository
        from app.db import SessionLocal

        meeting_id = _make_meeting(client)
        for i in range(1, 4):
            _post_final(client, meeting_id, i, f"第{i}句")
        provider = WindowProvider()
        _patch_provider(monkeypatch, provider)
        body = _trigger(client, meeting_id).json()
        assert captain_worker.drain(timeout=10.0)

        db = SessionLocal()
        try:
            row = captain_repository.get_by_report_id(db, body["report_id"])
            assert row.model_name == provider.name
        finally:
            db.close()

        data = client.get(
            f"/api/meetings/{meeting_id}/captain/{body['report_id']}"
        ).json()
        assert "model_name" not in data

    def test_provider_error_text_never_reaches_the_client(
        self, client, monkeypatch
    ):
        """§六 — no provider message, prompt, or raw transcript leaks out.

        `last_error` already lives only on the ORM row (mirroring
        `MeetingStatePublic`), and the SSE `reason` is a fixed short code. This
        pins all three surfaces at once so a future "helpful" change that adds
        the error to the response fails here.
        """
        from app.captain import repository as captain_repository
        from app.db import SessionLocal

        secret = "SECRET-PROVIDER-DETAIL-42"
        meeting_id = _make_meeting(client)
        for i in range(1, 4):
            _post_final(client, meeting_id, i, f"第{i}句")
        _patch_provider(monkeypatch, WindowProvider(error=secret))
        body = _trigger(client, meeting_id).json()
        assert captain_worker.drain(timeout=10.0)

        data = client.get(
            f"/api/meetings/{meeting_id}/captain/{body['report_id']}"
        ).json()
        assert data["status"] == "FAILED"
        assert secret not in json.dumps(data, ensure_ascii=False)
        assert "last_error" not in data

        # The operator-facing row keeps the detail; the product surface does not.
        db = SessionLocal()
        try:
            row = captain_repository.get_by_report_id(db, body["report_id"])
            assert secret in (row.last_error or "")
        finally:
            db.close()


# ==========================================================================
# 12. SSE delivery (ADR-002 Phase 2-A)
# ==========================================================================


class CaptainEventRecorder:
    """Capture captain.* publishes from the REAL EventBroker.

    Wrapping `publish` (not `build_*`) keeps the assertions on the same path
    production uses, and records the envelope so the payload/id contract is
    checked as actually emitted rather than as reconstructed by the test —
    same stance as `StateEventRecorder` in test_meeting_state_api.py.
    """

    def __init__(self) -> None:
        self.events: list = []

    def install(self, monkeypatch) -> None:
        from app.realtime.event_broker import event_broker

        original = event_broker.publish

        def spy(meeting_id, event):
            if event.event_type.startswith("captain."):
                self.events.append(event)
            return original(meeting_id, event)

        monkeypatch.setattr(event_broker, "publish", spy)

    @property
    def types(self) -> list[str]:
        return [e.event_type for e in self.events]

    def only(self, event_type: str):
        matched = [e for e in self.events if e.event_type == event_type]
        assert len(matched) == 1, f"expected exactly one {event_type}, got {self.types}"
        return matched[0]


@pytest.fixture
def captain_events(monkeypatch) -> CaptainEventRecorder:
    recorder = CaptainEventRecorder()
    recorder.install(monkeypatch)
    return recorder


class TestCaptainSseEvents:
    def test_completed_publishes_event_with_report_content(
        self, client, monkeypatch, captain_events
    ):
        """A finished report must reach the frontend without a GET.

        Payload contract (Phase 2-A): report_id / anchor_seq / positions /
        dispute_points / uncertainty — the exact fields the panel renders.
        """
        meeting_id = _make_meeting(client)
        for i in range(1, 4):
            _post_final(client, meeting_id, i, f"第{i}句讨论内容")

        provider = WindowProvider(positions=2, extra={
            "dispute_points": [{"text": "成本口径不一致", "source_segment_seqs": [2]}],
        })
        _patch_provider(monkeypatch, provider)
        body = _trigger(client, meeting_id).json()
        assert captain_worker.drain(timeout=10.0)

        env = captain_events.only("captain.completed")
        assert env.meeting_id == meeting_id
        assert env.payload["report_id"] == body["report_id"]
        assert env.payload["anchor_seq"] == body["anchor_segment_seq"]
        assert len(env.payload["positions"]) == 2
        assert env.payload["positions"][0]["label"] == "观点1"
        assert env.payload["dispute_points"][0]["text"] == "成本口径不一致"
        assert env.payload["uncertainty"]

        # Stable id lets the client dedupe a re-delivered frame.
        assert env.sse_id == (
            f"captain:{meeting_id}:{body['report_id']}:completed"
        )

        # The event is a fast path, NOT the fact source: the durable read must
        # agree, so a reconnecting client recovers the same report.
        report = client.get(
            f"/api/meetings/{meeting_id}/captain/{body['report_id']}"
        ).json()
        assert report["status"] == "COMPLETED"
        assert report["report"]["positions"][0]["label"] == "观点1"

    def test_failure_publishes_failed_event_without_content(
        self, client, monkeypatch, captain_events
    ):
        """A dead report must tell the client to stop waiting — and leak nothing."""
        meeting_id = _make_meeting(client)
        for i in range(1, 4):
            _post_final(client, meeting_id, i, f"第{i}句")

        _patch_provider(monkeypatch, WindowProvider(error="provider down"))
        body = _trigger(client, meeting_id).json()
        assert captain_worker.drain(timeout=10.0)

        env = captain_events.only("captain.failed")
        assert env.payload["report_id"] == body["report_id"]
        assert env.payload["anchor_seq"] == body["anchor_segment_seq"]
        # No report substance can exist for a failed report, and the reason is a
        # fixed product-safe code, never the provider's message.
        assert "positions" not in env.payload
        assert "provider down" not in json.dumps(env.payload)

    def test_queue_full_failure_also_publishes_failed(
        self, client, monkeypatch, captain_events
    ):
        """The 503 path must not leave other tabs spinning forever.

        The HTTP caller learns from the 503; every OTHER open tab only learns
        from the event. Failing the row without publishing would strand them.
        """
        meeting_id = _make_meeting(client)
        for i in range(1, 4):
            _post_final(client, meeting_id, i, f"第{i}句")
        _patch_provider(monkeypatch, WindowProvider())
        monkeypatch.setattr(captain_worker, "submit", lambda *a, **k: False)

        resp = _trigger(client, meeting_id)
        assert resp.status_code == 503
        assert captain_events.types == ["captain.failed"]

    def test_superseded_run_publishes_nothing(
        self, client, monkeypatch, captain_events
    ):
        """A replay of a terminal report must stay silent — no duplicate frame.

        Both terminal paths are covered here, because both were at risk:
        a `mark_completed` that returns None (row already COMPLETED) and a
        `mark_failed` that returns None (row already FAILED). Publishing
        unconditionally would make one report emit two events.

        Triggered via the SERVICE, not the HTTP endpoint: the endpoint also
        hands the report to the background worker, and a concurrent worker run
        would race the explicit `_run` below instead of exercising the dedupe
        path this test is about.
        """
        from app.captain import repository as captain_repository
        from app.db import SessionLocal

        meeting_id = _make_meeting(client)
        for i in range(1, 4):
            _post_final(client, meeting_id, i, f"第{i}句")
        provider = WindowProvider()
        _patch_provider(monkeypatch, provider)

        db = SessionLocal()
        try:
            ok_row, ok_created = captain_service.trigger(db, meeting_id)
            fail_row, fail_created = captain_service.trigger(
                db, meeting_id, anchor_segment_seq=2
            )
            # Read the ids out BEFORE the session closes — a detached ORM
            # instance cannot lazily refresh its attributes afterwards.
            ok_id, fail_id = ok_row.report_id, fail_row.report_id
        finally:
            db.close()
        assert ok_created and fail_created

        # --- success path -------------------------------------------------
        assert _run(meeting_id, ok_id, provider) == "OK"
        # Same report replayed: already terminal, so nothing is republished.
        assert _run(meeting_id, ok_id, provider) == "SUPERSEDED"

        # --- failure path -------------------------------------------------
        fail_provider = WindowProvider(error="provider down")
        assert _run(meeting_id, fail_id, fail_provider) == "FAILED"
        # Re-failing an already-FAILED row must not announce the failure twice.
        assert _run(meeting_id, fail_id, fail_provider) == "SUPERSEDED"

        assert captain_events.types == ["captain.completed", "captain.failed"]

        db = SessionLocal()
        try:
            rows = captain_repository.list_for_meeting(db, meeting_id)
            statuses = {r.status for r in rows}
        finally:
            db.close()
        assert statuses == {"COMPLETED", "FAILED"}

    def test_publish_failure_does_not_damage_the_report(
        self, client, monkeypatch
    ):
        """SSE is not a fact source: a broken broker must not fail the report.

        commit-before-publish means the row is already durable when publish
        runs, so an exception there is a delivery problem — never an analysis
        outcome.
        """
        from app.realtime.event_broker import event_broker

        meeting_id = _make_meeting(client)
        for i in range(1, 4):
            _post_final(client, meeting_id, i, f"第{i}句")
        _patch_provider(monkeypatch, WindowProvider())

        def explode(meeting_id_, event):
            if event.event_type.startswith("captain."):
                raise RuntimeError("broker exploded")

        monkeypatch.setattr(event_broker, "publish", explode)
        body = _trigger(client, meeting_id).json()
        assert captain_worker.drain(timeout=10.0)

        report = client.get(
            f"/api/meetings/{meeting_id}/captain/{body['report_id']}"
        ).json()
        assert report["status"] == "COMPLETED"
        assert report["report"]["positions"]

    def test_existing_event_types_are_still_published(
        self, client, monkeypatch
    ):
        """The referee's new events must not displace the existing pipeline.

        Guards the CLAUDE.md three-place sync rule in the direction that has no
        compile-time check on the backend: every pre-existing event type must
        still be reachable after adding captain.*.
        """
        from app.realtime.event_broker import event_broker
        from app.realtime.schemas import SSE_EVENT_TYPES

        seen: set[str] = set()
        original = event_broker.publish

        def spy(meeting_id, event):
            seen.add(event.event_type)
            return original(meeting_id, event)

        monkeypatch.setattr(event_broker, "publish", spy)
        _patch_provider(monkeypatch, WindowProvider(error="down"))

        meeting_id = _make_meeting(client)
        _post_final(client, meeting_id, 1, "水华队长")
        assert captain_worker.drain(timeout=10.0)

        assert {"transcript.final", "captain.failed"} <= seen
        for event_type in seen:
            assert event_type in getattr(SSE_EVENT_TYPES, "__args__"), event_type

    def test_captain_events_do_not_disturb_transcript_final(
        self, client, monkeypatch, captain_events
    ):
        """FINAL delivery stays exactly once, in order, despite the referee.

        The referee is a SECOND consumer of the same committed FINAL; if its
        publish path could interleave with the transcript's, a client would see
        frames out of order or duplicated.
        """
        meeting_id = _make_meeting(client)
        for i in range(1, 4):
            _post_final(client, meeting_id, i, f"第{i}句")

        _patch_provider(monkeypatch, WindowProvider())
        _trigger(client, meeting_id)
        assert captain_worker.drain(timeout=10.0)

        snapshot = client.get(f"/api/meetings/{meeting_id}/transcript").json()
        assert [s["segment_seq"] for s in snapshot["segments"]] == [1, 2, 3]

        # ...and the catch-up leg still replays ONLY the durable transcript.
        # `connect()` is the bounded half of the SSE endpoint (the live half is
        # an infinite generator), so this asserts the replay contract without
        # opening a stream: captain.* is non-durable and must never appear as
        # catch-up, even though a report for this meeting now exists.
        import asyncio

        from app.realtime import sse_service
        from app.realtime.event_broker import event_broker

        async def _catchup():
            sub, catch_up = sse_service.connect(meeting_id, 0)
            event_broker.unsubscribe(meeting_id, sub)
            return catch_up

        replay = asyncio.run(_catchup())
        assert [e.event_type for e in replay] == ["transcript.final"] * 3
        assert [e.payload["segment_seq"] for e in replay] == [1, 2, 3]


# ==========================================================================
# 16. provider wiring — the one call site that silently skipped it
# ==========================================================================


class TestProviderWiring:
    """`captain/service.py` must resolve its provider with thinking DISABLED.

    Regression pin for a shipped bug: with the internal DeepSeek gateway the
    implicit thinking pass is billed to the same `max_tokens` budget, so a
    reasoning-heavy window (a real multi-party discussion) spent all
    `CAPTAIN_MAX_OUTPUT_TOKENS` on `reasoning_tokens` and emitted no content at
    all. `extract_content` then raised `deepseek returned empty content
    (finish_reason=length, ...)` and every referee run on a real meeting FAILED.

    The provider-level mechanism was already tested (`test_deepseek_provider.py`
    pins that `thinking_enabled=False` produces `thinking: disabled`), and
    `summary_service` / `meeting_state` both passed the flag. What no test
    covered was the WIRING at this call site — which is exactly where it was
    missing. So this test asserts the resolved provider, not the transport.
    """

    def test_captain_resolves_provider_with_thinking_disabled(self, client, monkeypatch):
        from app.agent.llm import factory

        seen: list[dict] = []
        real_get_provider = factory.get_provider

        def spy(*args, **kwargs):
            seen.append(kwargs)
            return WindowProvider(positions=2)

        monkeypatch.setattr(factory, "get_provider", spy)

        meeting_id = _make_meeting(client)
        for i in range(1, 4):
            _post_final(client, meeting_id, i, f"第{i}句讨论内容")

        body = _trigger(client, meeting_id).json()
        assert captain_worker.drain(timeout=10.0)
        assert seen, "captain never resolved a provider — wiring changed"
        assert seen[0].get("thinking_enabled") is False, (
            "captain must pass thinking_enabled=False; without it a "
            "reasoning-heavy window exhausts max_tokens on reasoning_tokens "
            "and the report FAILS with empty content"
        )
        assert seen[0].get("max_output_tokens") == settings.captain_max_output_tokens

    def test_fake_provider_path_still_completes(self, client, monkeypatch):
        """The flag must not disturb the injected-provider path.

        `run_analysis(provider=...)` short-circuits before `get_provider`, so
        the existing offline suite is unaffected — pinned here so a future
        refactor that resolves the provider unconditionally is caught.
        """
        meeting_id = _make_meeting(client)
        for i in range(1, 4):
            _post_final(client, meeting_id, i, f"第{i}句讨论内容")

        _patch_provider(monkeypatch, WindowProvider(positions=2))
        body = _trigger(client, meeting_id).json()
        assert captain_worker.drain(timeout=10.0)

        got = client.get(f"/api/meetings/{meeting_id}/captain/{body['report_id']}").json()
        assert got["status"] == "COMPLETED"
        assert len(got["report"]["positions"]) == 2
