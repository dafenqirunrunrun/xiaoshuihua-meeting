"""产品化演示输入入口（POST /api/meetings/{id}/demo/utterances）回归套件。

这个入口存在的唯一理由是：让产品 UI 能打出**真实 FINAL**，而**不**把
``segment_seq`` 的所有权交给浏览器（CLAUDE.md 硬规则）。所以本套件的核心不是
「接口能返回 200」，而是钉住下面四条契约：

  1. **序号由服务端分配**：请求体里根本没有 ``segment_seq``；
     响应里回传的 seq 与真实 ASR 路径分配的一致（max+1）。
  2. **与真实链路同构**：demo 发言与 ``/asr-events`` 提交的发言进入同一张表、
     同样的 speaker 归一化、同样触发水华队长 —— 没有第二条实现。
  3. **唤醒判定在服务端**：``triggered_captain`` 如实反映
     ``captain/wake_word.py`` 的判定，前端不得自行推断。
  4. **失败隔离**：demo 入口关掉 / 会议状态不对 / 文本非法，都不影响
     既有链路，且错误语义与其它 ingress 一致（404 / 409 / 422）。

全程离线、fresh SQLite schema、``LLM_PROVIDER=fake``。
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.demo import service as demo_service
from app.demo.schemas import DemoUtteranceIn

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _make_meeting(client, *, status: str = "IN_PROGRESS") -> str:
    created = client.post("/api/meetings", json={"title": "演示输入测试会议"})
    meeting_id = created.json()["id"]
    if status != "CREATED":
        client.post(f"/api/meetings/{meeting_id}/start")
    return meeting_id


def _say(client, meeting_id: str, speaker: str, text: str, **extra):
    return client.post(
        f"/api/meetings/{meeting_id}/demo/utterances",
        json={"speaker": speaker, "text": text, **extra},
    )


@pytest.fixture(autouse=True)
def _reset_demo_speakers():
    """speaker 映射是进程级单例，跨测试必须清零，否则 id 会漂。"""
    demo_service.reset_speaker_ids()
    yield
    demo_service.reset_speaker_ids()


class RecordingProvider:
    """Provider that returns a minimal valid report, recording each call.

    The demo tests care about *whether the referee was asked*, not about prose.
    """

    name = "demo-recording-provider"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, prompt: str, **_kwargs) -> str:
        self.calls.append(prompt)
        return (
            '{"discussion_summary": "与会者讨论了方案取舍。",'
            ' "positions": [{"label": "推进", "statement": "应当继续推进当前方案",'
            ' "source_segment_seqs": [1]}],'
            ' "agreement_points": [], "dispute_points": [], "evidence": [],'
            ' "uncertainty": "窗口内容有限。", "suggestion": "补充分工与时间点。"}'
        )


def _patch_provider(monkeypatch, provider) -> None:
    from app.agent.llm import factory

    monkeypatch.setattr(factory, "get_provider", lambda **_kw: provider, raising=True)


# --------------------------------------------------------------------------
# A. 入口与序号所有权
# --------------------------------------------------------------------------


class TestDemoIngressContract:
    def test_demo_utterance_becomes_a_real_final(self, client):
        """一条演示发言就是一条持久化 FINAL —— 与真实 ASR 落在同一张表。"""
        meeting_id = _make_meeting(client)
        resp = _say(client, meeting_id, "产品", "我们优先优化检索速度。")

        assert resp.status_code == 200
        body = resp.json()
        assert body["segment_seq"] == 1
        assert body["speaker"] == "产品"
        assert body["triggered_captain"] is False

        snapshot = client.get(f"/api/meetings/{meeting_id}/transcript").json()
        assert len(snapshot["segments"]) == 1
        seg = snapshot["segments"][0]
        assert seg["segment_seq"] == 1
        assert seg["text"] == "我们优先优化检索速度。"
        # speaker 名 -> 不透明整数 -> 内部字符串形式（asr_adapter 归一化）
        assert seg["speaker_id"] == "speaker_0"

    def test_segment_seq_is_allocated_by_the_server_not_the_client(self, client):
        """请求体**没有** segment_seq 的位置；多轮发言序号严格递增。"""
        meeting_id = _make_meeting(client)
        seqs = [
            _say(client, meeting_id, "主持人", f"第 {i} 句。").json()["segment_seq"]
            for i in range(1, 5)
        ]
        assert seqs == [1, 2, 3, 4]

        # extra="forbid"：客户端试图自带 segment_seq 必须被拒绝，而不是被忽略。
        # 这是「浏览器不得拥有 ordering」这条规则在协议层的体现。
        rejected = client.post(
            f"/api/meetings/{meeting_id}/demo/utterances",
            json={"speaker": "产品", "text": "偷偷带序号", "segment_seq": 99},
        )
        assert rejected.status_code == 422

    def test_demo_and_real_asr_paths_share_one_ordering(self, client):
        """演示发言与 /asr-events 混投时，序号来自同一个分配器（无第二套逻辑）。"""
        meeting_id = _make_meeting(client)

        first = _say(client, meeting_id, "产品", "演示这条。").json()
        assert first["segment_seq"] == 1

        # 真实 ASR 协作方契约：不带 segmentSeq，由 Transcript Domain 分配
        asr = client.post(
            f"/api/internal/meetings/{meeting_id}/asr-events",
            json={
                "type": "transcript.final",
                "meetingId": meeting_id,
                "asrSessionId": "sess-real",
                "sentenceId": 7,
                "speakerId": 3,
                "startMs": 60_000,
                "endMs": 65_000,
                "text": "真实 ASR 这条。",
                "provider": "tencent",
            },
        )
        assert asr.status_code == 200

        second = _say(client, meeting_id, "产品", "演示再一条。").json()
        # 两路共用一个 max+1 分配器：真实那条吃掉 2，演示下一条拿 3。
        assert second["segment_seq"] == 3

        snapshot = client.get(f"/api/meetings/{meeting_id}/transcript").json()
        assert [s["segment_seq"] for s in snapshot["segments"]] == [1, 2, 3]

    def test_speaker_name_maps_to_a_stable_opaque_id(self, client):
        """同一个人多次发言 -> 同一个 speakerId；换人 -> 换 id。"""
        meeting_id = _make_meeting(client)
        a1 = _say(client, meeting_id, "产品", "第一次发言。").json()
        b1 = _say(client, meeting_id, "技术", "另一人的发言。").json()
        a2 = _say(client, meeting_id, "产品", "又轮到我。").json()

        assert a1["speaker_id"] == a2["speaker_id"] == 0
        assert b1["speaker_id"] == 1

        snapshot = client.get(f"/api/meetings/{meeting_id}/transcript").json()
        assert [s["speaker_id"] for s in snapshot["segments"]] == [
            "speaker_0",
            "speaker_1",
            "speaker_0",
        ]

    def test_timeline_is_monotonic_even_when_the_client_asks_to_go_backwards(self, client):
        """显式 start_ms 只能把时间往后推 —— 倒流会破坏窗口裁剪的正序前提。"""
        meeting_id = _make_meeting(client)
        first = _say(client, meeting_id, "产品", "第一句。", duration_ms=5000).json()
        assert (first["start_ms"], first["end_ms"]) == (0, 5000)

        # 试图回到更早：被钳到上一条的 end_ms，绝不接受时间倒流
        backwards = _say(
            client, meeting_id, "技术", "想回到过去。", start_ms=0, duration_ms=5000
        ).json()
        assert backwards["start_ms"] == 5000
        assert backwards["end_ms"] == 10000

        # 正常的向后：按上一条 end_ms 顺序接续（且自动补上默认时长）
        forwards = _say(client, meeting_id, "主持人", "接着往下说。").json()
        assert forwards["start_ms"] == 10000
        assert forwards["end_ms"] == 15000


# --------------------------------------------------------------------------
# B. 唤醒判定归服务端
# --------------------------------------------------------------------------


class TestDemoWakeWord:
    def test_wake_word_line_reports_triggered_captain(self, client):
        """含唤醒词的发言 -> triggered_captain=True；普通发言 -> False。"""
        meeting_id = _make_meeting(client)
        plain = _say(client, meeting_id, "产品", "我们先看成本。").json()
        assert plain["triggered_captain"] is False

        wake = _say(client, meeting_id, "技术", "水华队长，你怎么看？").json()
        assert wake["triggered_captain"] is True

    def test_quick_send_uses_the_server_side_wake_phrase(self, client):
        """快捷发送：不传 text，由服务端按自身配置组句 —— 必然唤醒。"""
        meeting_id = _make_meeting(client)
        resp = client.post(
            f"/api/meetings/{meeting_id}/demo/utterances",
            json={"speaker": "主持人", "trigger_captain": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["triggered_captain"] is True
        # 落库的就是服务端配置的唤醒词本身（此处为默认值）
        assert "水华队长" in body["text"]

        snapshot = client.get(f"/api/meetings/{meeting_id}/transcript").json()
        assert snapshot["segments"][0]["text"] == body["text"]

    def test_quick_send_follows_a_reconfigured_wake_phrase(self, client, monkeypatch):
        """把唤醒词改成别的措辞，快捷发送跟着变 —— 前端不会发一句唤不醒的话。

        这是「快捷按钮不硬编码唤醒词」这条设计的**回归钉**：如果哪天有人把
        唤醒词写回前端常量，这个测试会红。
        """
        monkeypatch.setattr(settings, "captain_wake_words", "小水华请发言,备用词")
        meeting_id = _make_meeting(client)
        body = client.post(
            f"/api/meetings/{meeting_id}/demo/utterances",
            json={"speaker": "主持人", "trigger_captain": True},
        ).json()
        assert body["text"] == "小水华请发言"
        assert body["triggered_captain"] is True

    def test_quick_send_respects_the_configured_phrase_in_orchestration(
        self, client, monkeypatch
    ):
        """端到端：换了唤醒词后，快捷发送经咽喉点同样真的触发了裁判。"""
        from app.agent.llm import factory

        provider = RecordingProvider()
        monkeypatch.setattr(factory, "get_provider", lambda **_kw: provider, raising=True)
        monkeypatch.setattr(settings, "captain_wake_words", "小水华请发言")

        meeting_id = _make_meeting(client)
        _say(client, meeting_id, "产品", "先看检索速度。")
        _say(client, meeting_id, "技术", "先看准确率。")
        client.post(
            f"/api/meetings/{meeting_id}/demo/utterances",
            json={"speaker": "主持人", "trigger_captain": True},
        )

        from app.captain.worker import captain_worker

        assert captain_worker.drain(timeout=10.0)
        assert len(provider.calls) == 1
        assert client.get(f"/api/meetings/{meeting_id}/captain/latest").json()[
            "status"
        ] == "COMPLETED"

    def test_quick_send_variants_and_explicit_text_stay_mutually_exclusive(
        self, client
    ):
        """显式 text 优先于 trigger_captain —— 但两者都没内容时拒绝。"""
        meeting_id = _make_meeting(client)
        # 有 text 时按 text 走（即使 trigger_captain=true）
        with_text = client.post(
            f"/api/meetings/{meeting_id}/demo/utterances",
            json={"speaker": "产品", "text": "普通发言。", "trigger_captain": True},
        ).json()
        assert with_text["text"] == "普通发言。"
        assert with_text["triggered_captain"] is False

        # 既没 text 也没 trigger_captain → 没有内容可落库
        assert client.post(
            f"/api/meetings/{meeting_id}/demo/utterances", json={"speaker": "产品"}
        ).status_code == 422
        # 只有空格也算「没给」
        assert _say(client, meeting_id, "产品", "   ").status_code == 422

    def test_wake_word_line_actually_reaches_the_referee(self, client, monkeypatch):
        """端到端：唤醒发言经既有咽喉点真的触发了裁判，并落了一份报告。"""
        provider = RecordingProvider()
        _patch_provider(monkeypatch, provider)

        meeting_id = _make_meeting(client)
        _say(client, meeting_id, "产品", "优先优化检索速度。")
        _say(client, meeting_id, "技术", "优先提升准确率。")
        _say(client, meeting_id, "主持人", "水华队长，帮我们梳理一下。")

        # 走真实路径：transcript 咽喉点 -> captain 触发 -> 独立 worker。
        from app.captain.worker import captain_worker

        assert captain_worker.drain(timeout=10.0)

        assert len(provider.calls) == 1, "唤醒词应当且只应当触发一次裁判"
        latest = client.get(f"/api/meetings/{meeting_id}/captain/latest")
        assert latest.status_code == 200
        assert latest.json()["status"] == "COMPLETED"


# --------------------------------------------------------------------------
# C. 失败隔离与错误语义
# --------------------------------------------------------------------------


class TestDemoIngressBoundaries:
    def test_disabled_demo_ingress_removes_the_route(self, client, monkeypatch):
        """关闭开关 -> 404（能力不存在），而不是 403（权限不足）。"""
        monkeypatch.setattr(settings, "demo_ingress_enabled", False)
        meeting_id = _make_meeting(client)
        assert _say(client, meeting_id, "产品", "还发得出去吗？").status_code == 404

    def test_created_meeting_rejects_demo_utterance(self, client):
        """会议未开始不接受发言 —— 与真实 ingress 的 409 语义一致。"""
        meeting_id = _make_meeting(client, status="CREATED")
        assert _say(client, meeting_id, "产品", "会议还没开始。").status_code == 409

    def test_unknown_meeting_is_404(self, client):
        assert _say(client, "no-such-meeting", "产品", "喂？").status_code == 404

    def test_blank_text_is_rejected(self, client):
        """空文本不得落库：min_length=1 是契约，不是 UI 的礼貌。"""
        meeting_id = _make_meeting(client)
        assert _say(client, meeting_id, "产品", "").status_code == 422

    def test_demo_failure_does_not_disturb_the_transcript(self, client, monkeypatch):
        """入口报错后，已落库的转写与后续真实提交必须完好无损。"""
        meeting_id = _make_meeting(client)
        _say(client, meeting_id, "产品", "这条是好的。")

        # 非法请求（空文本）— 绝不破坏任何东西
        _say(client, meeting_id, "产品", "")

        after = _say(client, meeting_id, "技术", "这条也要能进去。")
        assert after.status_code == 200
        assert after.json()["segment_seq"] == 2
        snapshot = client.get(f"/api/meetings/{meeting_id}/transcript").json()
        assert [s["segment_seq"] for s in snapshot["segments"]] == [1, 2]

    def test_payload_meeting_id_field_is_not_accepted(self, client):
        """演示契约里没有 meetingId —— meeting 只从 path 取，避免两个事实源。"""
        meeting_id = _make_meeting(client)
        rejected = client.post(
            f"/api/meetings/{meeting_id}/demo/utterances",
            json={"speaker": "产品", "text": "带错会议号", "meetingId": "other"},
        )
        assert rejected.status_code == 422


# --------------------------------------------------------------------------
# D. 服务层直接调用（不经 HTTP）：确认它不依赖 FastAPI
# --------------------------------------------------------------------------


class TestDemoServiceLayer:
    def test_service_is_callable_without_a_background_tasks_object(self, client):
        """service 层可按需直接调用（BackgroundTasks 是可选转发，不是前置）。"""
        from app.db import SessionLocal

        meeting_id = _make_meeting(client)
        db = SessionLocal()
        try:
            out = demo_service.ingest_demo_utterance(
                db, meeting_id, DemoUtteranceIn(speaker="产品", text="直接调用。")
            )
        finally:
            db.close()
        assert out.segment_seq == 1
        assert out.speaker_id == 0
