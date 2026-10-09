"""演示输入应用层 —— 把一条「演示发言」变成真实 FINAL（ADR-002 §6 产品化入口）。

    POST /api/meetings/{id}/demo/utterances
      -> demo_schemas.DemoUtteranceIn           (产品级 DTO：speaker + text)
      -> demo_service.ingest_demo_utterance()   (THIS module)
           1. meeting 存在 + 状态校验（复用既有 domain 异常）
           2. speaker 名 -> 不透明 speakerId（进程内稳定映射）
           3. 时间轴推进（start_ms 省略时接上一条 endMs）
           4. 构造 AsrTranscriptEvent（外部协作方契约）
           5. asr_service.ingest_asr_event()      (既有 ingress，UNCHANGED)

为什么复用 ``asr_service`` 而不是自己写一遍
--------------------------------------------

``asr_service.ingest_asr_event`` 是**真实 ASR 走的那条路**：它在 meeting lock 内
分配 ``segment_seq``、处理幂等、调用既有的 ``transcript_service.handle_event``。
演示发言走同一条路意味着：

  - 「演示」与「真实」在 ordering / 幂等 / SSE / Meeting State / 水华队长唤醒
    上没有第二条实现可以漂移 —— 这是本设计最重要的性质；
  - 本模块**不碰** ``segment_seq``（唯一由 Transcript Domain 分配，CLAUDE.md 硬规则）、
    不碰 transcript schema、不新增列、不写旁路表。

``sentenceId`` 的推导
---------------------

``asr_schemas`` 要求 ``sentenceId`` 是**单次 ASR session 内**的句序，而
``utterance_id = f"{asrSessionId}:{sentenceId}"`` 必须全局唯一（``UNIQUE(event_id)``
是全局的，event_id 里混入了 meeting_id）。

这里用**当前持久化的 max(segment_seq) + 1** 作为 sentenceId：每个序号在 meeting
内只被分配一次，且分配发生在同一把 meeting lock 内，因此：

  - utterance_id 永不碰撞（无需额外计数器状态，进程重启后依然正确）；
  - 重试语义自然正确 —— 重复提交同一序号会命中 Case A 幂等并被吸收，
    与重发同一条 ASR FINAL 的行为完全一致。

``asrSessionId`` 只是每条演示发言一个的 opaque 作用域标签，不承载语义。
"""

from __future__ import annotations

import uuid
from threading import RLock

from sqlalchemy.orm import Session

from app.common.exceptions import InvalidMeetingStateTransition, MeetingNotFoundError
from app.common.meeting_lock import meeting_lock
from app.demo.schemas import MAX_SPEAKER_ID, DemoUtteranceAccepted, DemoUtteranceIn
from app.meeting.meeting_model import Meeting
from app.transcript import asr_schemas
from app.transcript import asr_service
from app.transcript import repository as transcript_repository

# 「唤醒词」这件事只有一处实现（纯函数，无 IO / 无 DB / 无 LLM）。
# 本模块只用它来**如实回报**「这句话有没有请求裁判」，不参与裁判编排。
from app.captain import wake_word

# 演示发言归属的 provider 标签。与真实 ASR 区分开，方便日志排查；
# 它不被持久化（asr_adapter 明确只用于 identity 推导），故无 schema 影响。
_DEMO_PROVIDER = "demo"

# 演示 session 前缀：仅为了让 asrSessionId 可读，不承载任何业务语义。
_SESSION_PREFIX = "demo"

# 接受演示发言的会议状态（与 transcript_service 的 ingest gate 同一语义）。
# 这里独立列出而非 import，是为了让本模块的校验点不依赖 captain 枚举的演化；
# 真正的状态 gate 仍在 transcript_service 内（唯一事实源）。
_ACCEPTING_STATUSES = frozenset({"IN_PROGRESS", "FINALIZING"})

# 进程内 speaker 名 -> 不透明 speakerId 的映射。按**出现顺序**分配而非哈希：
# 哈希会撞（两个名字落同一个 id），顺序分配天然单射。上限由契约（MAX_SPEAKER_ID）
# 兜底，真实演示远达不到。
_speaker_lock = RLock()
_speaker_ids: dict[str, int] = {}


def _resolve_speaker_id(speaker: str) -> int:
    """Map a display name onto a stable opaque int for this process."""
    with _speaker_lock:
        existing = _speaker_ids.get(speaker)
        if existing is not None:
            return existing
        assigned = len(_speaker_ids)
        if assigned > MAX_SPEAKER_ID:  # 防御：绝不构造越界 DTO
            raise InvalidMeetingStateTransition(
                "too many distinct demo speakers for this process"
            )
        _speaker_ids[speaker] = assigned
        return assigned


def reset_speaker_ids() -> None:
    """Clear the process-local speaker map (tests / shutdown)."""
    with _speaker_lock:
        _speaker_ids.clear()


def ingest_demo_utterance(
    db: Session,
    meeting_id: str,
    payload: DemoUtteranceIn,
    *,
    background_tasks=None,
) -> DemoUtteranceAccepted:
    """Append one demo utterance as a real FINAL. Returns the server's projection.

    Raises the same domain exceptions as the internal ingress
    (MeetingNotFoundError -> 404, InvalidMeetingStateTransition / conflicts ->
    409), so the existing exception handlers apply unchanged.
    """
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise MeetingNotFoundError(meeting_id)
    if meeting.status not in _ACCEPTING_STATUSES:
        raise InvalidMeetingStateTransition(
            f"meeting {meeting_id} status {meeting.status} does not accept "
            f"demo utterances (only {sorted(_ACCEPTING_STATUSES)})"
        )

    speaker_id = _resolve_speaker_id(payload.speaker)

    # 快捷发送时文本由服务端组句（见 DemoUtteranceIn 文档）：前端硬编码唤醒词
    # 会在部署改了 CAPTAIN_WAKE_WORDS 后静默失效。
    content = payload.resolved_text(wake_phrase=_wake_phrase())

    # 读 max + 分配句序在同一临界区。这里**只读** transcript——真正的 seq 分配
    # 在 asr_service 内（也是 meeting lock），两者是同一把 RLock，同线程重入，
    # 因此从读 max 到落库没有可插入的窗口。
    with meeting_lock(meeting_id):
        previous_max_seq = transcript_repository.max_seq(db, meeting_id) or 0
        previous = (
            transcript_repository.get_by_seq(db, meeting_id, previous_max_seq)
            if previous_max_seq > 0
            else None
        )
        previous_end_ms = (
            previous.end_ms
            if previous is not None and previous.end_ms is not None
            else None
        )

    # 时间轴单调：captain/context.py 依赖 ASR 相对时间的正序性来裁剪窗口。
    # 显式 start_ms 只被当作**下界**，绝不接受让时间倒流的输入。
    if payload.start_ms is not None:
        start_ms = max(payload.start_ms, previous_end_ms or 0)
    else:
        start_ms = previous_end_ms or 0
    end_ms = start_ms + payload.duration_ms

    # sentenceId：meeting 内单调，与 seq 分配共用同一把锁 -> 不会碰撞。
    sentence_id = previous_max_seq + 1

    external = asr_schemas.AsrTranscriptEvent(
        type="transcript.final",
        meetingId=meeting_id,
        asrSessionId=f"{_SESSION_PREFIX}-{uuid.uuid4().hex[:12]}",
        sentenceId=sentence_id,
        speakerId=speaker_id,
        startMs=start_ms,
        endMs=end_ms,
        text=content,
        provider=_DEMO_PROVIDER,
    )
    # 走真实 ingress：ordering / 幂等 / SSE / 唤醒检测全部复用同一条实现。
    internal = asr_service.ingest_asr_event(
        db, meeting_id, external, background_tasks=background_tasks
    )

    # 服务端判定是否请求了水华队长。唤醒规则唯一存在于 captain/wake_word.py，
    # 前端绝不自行推断（该模块文档已说明原因：两份实现必然漂移，而漏判是静默的）。
    triggered = _matches_wake_word(content)

    return DemoUtteranceAccepted(
        meeting_id=meeting_id,
        segment_seq=internal.segment_seq or 0,
        utterance_id=internal.utterance_id,
        speaker=payload.speaker,
        speaker_id=speaker_id,
        start_ms=start_ms,
        end_ms=end_ms,
        text=content,
        triggered_captain=triggered,
    )


def _wake_phrase() -> str:
    """The first configured wake phrase, used as the quick-send text.

    Reads only the FIRST entry: quick-send is a button labelled 「水华队长」, and
    emitting every configured variant would append several wake words in one
    utterance. Falls back to the literal only if settings is somehow empty —
    the same string the default config carries.
    """
    words = wake_word.configured_wake_words()
    return words[0] if words else "水华队长"


def _matches_wake_word(text: str) -> bool:
    """Delegate to the single wake-word implementation.

    Imported inside the function on purpose (see `app/demo/__init__.py`): this
    package must not pull the captain package in at import time. Only the PURE
    `wake_word` function is touched — never the service, worker or schemas, so
    the demo ingress never participates in referee orchestration.
    """
    return wake_word.match(text)


__all__ = ["ingest_demo_utterance", "reset_speaker_ids"]
