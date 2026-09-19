# 通信协议（Source of Truth）

本目录是**跨模块通信契约的唯一事实源**。ESP32 / 上游 ASR / 后端 / 前端之间共享的
传输约定与事件格式，一律以本目录为准。

> 状态：**ASR-0B Contract Freeze**（2026-09-19）。
> 下列文档中标记 ✅ FROZEN 的条目已冻结；标记 ⏳ PENDING 的条目双方尚未确认，
> 实现方**不得**单方面按自己的假设锁定。

---

## 优先级

与其他文档冲突时：

```text
prd.md  >  docs/decisions/ (Accepted ADR)  >  docs/architecture.md
        >  protocols/*.md（本目录）  >  docs/api-contract.md
        >  现有实现
```

本目录描述的是**跨团队契约**；`docs/api-contract.md` 描述的是**我方后端实际实现**。
两者不一致时视为缺陷，必须修文档或修代码，不允许"文档各说各话"。

---

## 文档索引

| 文档 | 内容 | 状态 |
| --- | --- | --- |
| [transcript-format.md](./transcript-format.md) | 上游 ASR → 后端的字幕事件契约（外部 DTO）、utterance / event identity、排序归属 | ✅ FROZEN |
| [realtime-events.md](./realtime-events.md) | SSE 增量流帧格式、事件名、恢复游标、`TRANSCRIPT_STREAM_END` 终止契约 | ✅ FROZEN |
| [meeting-api.md](./meeting-api.md) | 会议生命周期四态机与生命周期接口 | ✅ FROZEN |
| [esp32-audio.md](./esp32-audio.md) | ESP32-S3 音频上行链路 | ⏸ NOT IN SCOPE（V0.2） |

---

## 契约的三条铁律

1. **不猜**。契约里没有的字段，实现方不得自行添加；契约里有的字段，不得悄悄忽略。
   未知字段一律**大声失败**（`422`），不做静默兼容。
2. **归属清晰**。每个字段都能回答"谁产生、谁消费、谁负责"。产生方变更语义 =
   破坏性变更，必须先改本目录再改代码。
3. **PENDING 不实现**。尚未确认的条目（尤其是真实 STREAM_END 的收发双方）不得写死
   进生产代码；MVP 阶段用内部 mock 仅作回归用途，且必须在代码注释里写明
   "这不是最终协作契约"。

---

## 变更流程

1. 先改本目录（含变更理由、兼容性影响、受影响方）。
2. 双方确认后标记 ✅ FROZEN。
3. 再改实现与 `docs/api-contract.md`。
4. 补 `backend/tests/` 回归用例。

破坏性变更必须同步给出**迁移说明**（旧字段如何废弃、过渡期多长）。
