"""演示输入契约 —— 产品级「模拟参会人发言」入口。

存在理由
--------

产品需要一个能让**真人**在会议室页面打字模拟多人对话的入口（演示 / 试用 /
验收）。这件事本可以用两个已有入口之一，但都不合适：

  ``POST /api/internal/meetings/{id}/transcript-events``
      INTERNAL 契约，**producer-owned ``segment_seq``**。让浏览器调它等于把
      meeting-wide transcript ordering 的所有权交给前端 —— 直接违反
      CLAUDE.md「segment_seq 由 Backend 生成」。不可用。

  ``POST /api/internal/meetings/{id}/asr-events``
      契约本身没问题（无 ``segmentSeq``，由 Transcript Domain 分配），
      但它是**协作方**（Audio / Tencent ASR）的入口，identity 要
      ``asrSessionId`` / ``sentenceId`` / ``provider``。让产品 UI 走协作方契约
      会把「谁在说话」这件事伪装成 ASR session —— 那是别人的语义，不是我们的。

所以这里新增第三个、**极薄**的入口：``speaker`` 直接是业务概念，
``segment_seq`` / ``utterance_id`` / ``event_id`` 全部由本模块派生，
然后原封不动交给**既有的** ``asr_service.ingest_asr_event()`` —— 与真实
ESP32 / Tencent ASR **完全同一条代码路径**，因此：

  - ordering 分配、幂等、UNIQUE 冲突、SSE 广播、Meeting State 调度、
    水华队长唤醒检测，全部行为一致，不存在「演示路径」与「真实路径」漂移；
  - 本模块**不碰** transcript schema、不新增列、不写任何业务表。

边界
----

  - 只写 transcript（经既有 service），绝不写 meeting 四态机。
  - ``speaker`` 只决定 ``speaker_id`` 这个不透明整数；与 person attribution
    无关 —— ADR-002 明确禁止裁判报告承载人名/身份，而
    ``ASR_SPEAKER_LABELS_ENABLED=false`` 时 ``speaker_id`` 本就不可靠。
    这个字段的存在是为了让**转写列表**看起来像多人对话，不是为了识别谁。
  - 关闭 ``demo_ingress_enabled`` 时整个入口 404，主链路行为零变化。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

# 与 AsrTranscriptEvent.speakerId 的上界保持一致（见该字段定义）。
MAX_SPEAKER_ID = 1_000_000


class DemoUtteranceIn(BaseModel):
    """一条演示发言 —— 或一次「请求水华队长」的快捷发送。

    两种互斥模式：

      ``text`` 给定          普通发言。是否唤醒裁判由 ``wake_word.py`` 判定，
                             本契约不做任何匹配（唤醒规则只有一处实现）。
      ``trigger_captain``    快捷发送：文本由**服务端**用自己配置的唤醒词填入。

    第二种模式的存在理由：快捷按钮若在前端硬编码「水华队长」，部署一旦通过
    ``CAPTAIN_WAKE_WORDS`` 改了措辞，按钮就会发一句**唤不醒**的话 —— 而且失败
    是静默的（没有任何报错，只是裁判不出现）。让服务端用自己的配置组句，
    这件事就不可漂移，前端也不需要知道唤醒词是什么。
    """

    model_config = ConfigDict(extra="forbid")

    speaker: str = Field(
        ...,
        min_length=1,
        max_length=32,
        description="演示用发言人标识（如「主持人」「产品」）。仅映射为不透明 speakerId。",
    )
    text: str | None = Field(
        default=None,
        max_length=4000,
        description="发言内容。trigger_captain 为 true 时可省略（文本由服务端唤醒词填入）。",
    )
    trigger_captain: bool = Field(
        default=False,
        description="快捷发送：让服务端以自身配置的唤醒词作为发言内容。",
    )
    # 可选：覆盖 ASR 相对时间轴上的起点。省略时由服务端按上一条演示发言的
    # endMs 顺序推进，保证时间轴单调 —— 前端不必、也不应该自己算时间。
    start_ms: int | None = Field(default=None, ge=0)
    duration_ms: int = Field(
        default=5000,
        ge=1,
        le=600_000,
        description="未显式给 start_ms 时，本句占用的时长（毫秒）。",
    )

    @model_validator(mode="after")
    def _require_content_source(self) -> "DemoUtteranceIn":
        """必须是两种模式之一，否则没有内容可落库。

        空白 ``text`` 视为「没给」—— ``min_length`` 管不住 ``"   "``，而落一条
        只有空格的 FINAL 既污染转写又浪费一次裁判的窗口预算。
        """
        has_text = bool((self.text or "").strip())
        if not has_text and not self.trigger_captain:
            raise ValueError("either a non-blank text or trigger_captain=true is required")
        return self

    def resolved_text(self, *, wake_phrase: str) -> str:
        """The content to persist — server-owned when in quick-send mode.

        ``wake_phrase`` is injected by the service from settings rather than read
        here, so this DTO stays a pure contract with no settings dependency.
        """
        stripped = (self.text or "").strip()
        if stripped:
            return stripped
        return wake_phrase


class DemoUtteranceAccepted(BaseModel):
    """200 body：服务端**唯一事实源**投影，前端据此渲染时间与序号。

    ``segment_seq`` 回传是刻意的：它由 Transcript Domain 分配，客户端只能被告知，
    不能被允许自行指定。这也是本接口与 INTERNAL 契约最本质的区别。
    """

    meeting_id: str
    segment_seq: int
    utterance_id: str
    speaker: str
    speaker_id: int
    start_ms: int
    end_ms: int
    text: str
    # 该 FINAL 是否触发水华队长（服务端判定，前端绝不自行推断 —— 见
    # wake_word.py 模块文档）。快捷发送模式下必然为 true。
    triggered_captain: bool


__all__ = ["DemoUtteranceIn", "DemoUtteranceAccepted", "MAX_SPEAKER_ID"]
