# 字幕格式协议（Transcript Format）

> 状态：**✅ FROZEN**（ASR-0B，2026-09-19）
> 适用：上游 Audio / ASR 流水线 → 后端 Transcript Domain
> 对应实现：`backend/app/transcript/asr_schemas.py`、`asr_adapter.py`、`asr_service.py`

---

## 1. 边界

```text
Audio / ASR（厂商无关）
  ↓  归一化 PARTIAL / FINAL          ← 上游负责
[ 本协议：外部 DTO，camelCase ]      ← 契约边界，唯一跨团队面
  ↓
Adapter（纯字段翻译 + identity 派生）
  ↓
Internal Transcript Domain（snake_case）
  ↓
Transcript 持久化 / SSE / React
```

**上游不负责**：`segmentSeq`（会议内排序）、DB id、repository、SSE、前端、Summary。
**上游不需要也不应该**反向查询后端生成的排序结果。

---

## 2. 端点

```text
POST /api/internal/meetings/{meeting_id}/asr-events
```

`meeting_id` 必须与 payload `meetingId` 一致，否则 `400`。

---

## 3. 事件格式 ✅ FROZEN

PARTIAL（说话人可能未知）：

```json
{
  "type": "transcript.partial",
  "meetingId": "meeting_001",
  "asrSessionId": "sess-001",
  "sentenceId": 2,
  "speakerId": -1,
  "startMs": 950,
  "endMs": 6480,
  "text": "我们今天讨论会议助手",
  "provider": "tencent"
}
```

FINAL（同一 `sentenceId`，说话人通常已解析为 `>= 0`）：

```json
{
  "type": "transcript.final",
  "meetingId": "meeting_001",
  "asrSessionId": "sess-001",
  "sentenceId": 2,
  "speakerId": 0,
  "startMs": 950,
  "endMs": 7900,
  "text": "我们今天讨论会议助手的整体方案。",
  "provider": "tencent"
}
```

### 字段表 ✅ FROZEN

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `type` | str | `transcript.partial` \| `transcript.final` | 事件判别符 |
| `meetingId` | str | 1..64，须等于 path | 会议标识 |
| `asrSessionId` | str | **1..48** | ASR 会话标识，见 §6 |
| `sentenceId` | int | `>= 0`，`<= 2147483647` | **会话内**句子编号，**不等于**排序 |
| `speakerId` | int | `>= -1` | **-1 = 说话人暂未知**，合法 |
| `startMs` / `endMs` | int | `>= 0` 且 `endMs >= startMs` | 会话内相对偏移，见 §5 |
| `text` | str | `<= 4000` | 识别文本 |
| `provider` | str | 1..64 | **不透明**，不得据此硬编码厂商逻辑 |

**没有** `segmentSeq`，**没有** `utteranceId`，**没有** `eventId`——这三者归后端。

### 校验 ✅ FROZEN

- `extra="forbid"`：未知字段（如误传 `segmentSeq` 或 snake_case 写法）→ **422**。
- `endMs < startMs` → **422**。
- 契约漂移必须**大声失败**，不做静默兼容。

---

## 4. Identity 归属 ✅ FROZEN

| 概念 | 归属 | 派生方式 | 用途 |
| --- | --- | --- | --- |
| `utterance_id` | 后端 | `f"{asrSessionId}:{sentenceId}"` | PARTIAL 原地覆盖；PARTIAL→FINAL 交接 |
| FINAL `event_id` | 后端 | 确定性摘要（含 `meetingId`） | 幂等（全局 `UNIQUE(event_id)`） |
| PARTIAL `event_id` | 后端 | 摘要**混入 revision 内容** | 不做该句固定 id（否则后续 revision 会撞幂等） |
| `segment_seq` | 后端 | `max(segment_seq) + 1`（meeting lock 内） | **会议内唯一排序** |

**禁止 `segment_seq = sentenceId`**：`sentenceId` 是会话内编号，ASR 重连后会重号，
直接映射会撞 `UNIQUE(meeting_id, segment_seq)`。

**FINAL 幂等重试复用已存 `segment_seq`**，不消耗新号。

---

## 5. 时间轴语义 ✅ FROZEN（MVP）

- `startMs` / `endMs` 是**每个 ASR session 内的相对偏移**。
- **跨 session 不可比较**（重连后上游可能把时间轴归零）。
- 因此：**排序永远只认 `segment_seq`**；`start_ms` / `end_ms` **只用于存储、回显与展示**，
  绝不参与排序或完整性判断。
- **MVP 不做 rebase**（不把多 session 时间轴重建为会议全局时间轴）。

> ⏳ **V0.2 known limitation / tech debt**：会议级全局时间轴重建需要先持久化 session
> 元数据（`provider` / `asrSessionId` / `sentenceId` 当前只活在 ingress 生命周期内，
> **不落库**）。届时需 DB schema 变更，属于 V0.2 范围。

---

## 6. `asrSessionId` 上限 48 —— 为什么是硬约束 ✅ FROZEN

`utterance_id = f"{asrSessionId}:{sentenceId}"` 必须落在内部 64 字符界内：

```text
最坏情况 = 48 + 1 + len("2147483647") = 48 + 1 + 10 = 59  <=  64
```

超过 48 → **422**。这样派生 identity **精确且单射**；相比之下静默哈希或截断一个
identity 是不可接受的。该约束已同步暴露在 OpenAPI schema（`maxLength: 48`）。

---

## 7. 状态码 ✅ FROZEN

| 码 | 含义 |
| --- | --- |
| `200` | 接收成功（FINAL 已持久化 / 幂等命中；PARTIAL 已入实时缓存） |
| `400` | path `meeting_id` ≠ payload `meetingId` |
| `404` | meeting 不存在 |
| `409` | meeting 状态不允许；同句内容漂移（绝不静默覆盖）；**utterance 已 FINAL 关闭后收到 PARTIAL** |
| `422` | payload 校验失败（含未知字段） |

---

## 8. 顺序与重复语义 ✅ FROZEN

| 场景 | 行为 |
| --- | --- |
| **乱序到达**（先 sentence 3 后 sentence 1） | 均 `200`；`segment_seq` 按**到达顺序**分配 |
| **重复 FINAL**（内容相同） | `200` 幂等；不新增 segment |
| **同句 FINAL 内容漂移** | `409`，保留原值，绝不覆盖 |
| **迟到重试**（该句 FINAL 已落库后重发） | `200`，复用原 `segment_seq` |
| **PARTIAL 原地更新**（同句多次 revision） | 每次 `200`，覆盖实时缓存（不写库） |
| **PARTIAL 晚于同句 FINAL** | **`409`**：utterance 一旦 FINAL 即**关闭**，见 §9 |
| **多 session 重连**（`sentenceId` 从 1 重启） | 均 `200`；`utterance_id` 含 session 前缀，身份不串 |

---

## 9. Utterance 关闭语义 ✅ FROZEN

**同一 utterance 一旦收到 FINAL，即视为 closed。**

此后同 `utterance_id` 的 PARTIAL → **`409`**，不写入实时缓存。

理由：若接受，前端会渲染一条永远无法收敛的"识别中"行——它等待的 FINAL 早已到达，
且不会再作为 live 帧重发。按既有"契约漂移必须大声失败"原则，此情形应让调用方获知。

> 该不变式在**两个 ingress 上都生效**（领域不变式，非路由细节）。

---

## 10. 明确不做（本协议范围外）

- 真实腾讯 ASR 对接、音频采集、ESP32 固件（V0.2）。
- `provider` / `asrSessionId` / `sentenceId` 落库（见 §5 tech debt）。
- 多 worker / 多实例下的排序分配：当前 `segment_seq` 分配依赖**进程内 per-meeting
  `RLock`**，仅 MVP 单进程成立。多实例需升级为数据库级安全分配机制（V0.2）。
