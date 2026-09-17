# 小水华会议助手 MVP V0.1 API Contract

> 本文档记录**实际实现**后的契约，保持 `docs = actual implementation`。
> 更新于：Step 3 完成。若实现与本文档不一致，以本文档为准并同步代码。

所有响应错误统一为 `{"detail": "..."}`；错误码语义见各接口。

---

## 1. Business API（`/api`）

### POST /api/meetings — 创建会议

Request（PRD §37，仅 title + 可选 description）：

```json
{ "title": "小水华 MVP 方案讨论", "description": "可选" }
```

Response `201 / Meeting`：

```json
{
  "id": "meeting_abc123",
  "title": "小水华 MVP 方案讨论",
  "description": null,
  "status": "CREATED",
  "started_at": null, "ended_at": null, "completed_at": null,
  "created_at": "2026-09-17T02:04:34", "updated_at": "2026-09-17T02:04:34"
}
```

---

### POST /api/meetings/{meeting_id}/start

- `CREATED → IN_PROGRESS`，记录 `started_at`。
- 幂等：已是 `IN_PROGRESS` → 返回当前 Meeting，**不**改 `started_at`。
- `FINALIZING / COMPLETED` → **409**。
- Meeting 不存在 → **404**。

### POST /api/meetings/{meeting_id}/finish

- `IN_PROGRESS → FINALIZING`，记录 `ended_at`。
- 幂等：已是 `FINALIZING` → 返回当前，不改 `ended_at`；已是 `COMPLETED` → 幂等 no-op。
- `CREATED` → **409**。Meeting 不存在 → **404**。

### GET /api/meetings/{meeting_id}

返回单个 Meeting；不存在 → **404**。

### GET /api/meetings/{meeting_id}/transcript — Transcript Snapshot

返回**仅 FINAL** segments，按 `segment_seq ASC` 排序（PRD §32/§48，arch §6）：

```json
{
  "meeting_id": "meeting_abc123",
  "segments": [
    { "segment_seq": 1, "utterance_id": "utt_1", "speaker_id": "sp1",
      "text": "…", "start_ms": 0, "end_ms": 1500 },
    { "segment_seq": 2, "utterance_id": "utt_2", "speaker_id": "sp2",
      "text": "…", "start_ms": 1500, "end_ms": 3000 }
  ]
}
```

PARTIAL 永远不会出现在此接口。Meeting 不存在 → **404**。

---

## 2. Internal Transcript Ingress（`/api/internal`）

> 供 ASR / Mock Transcript Producer 调用；是跨团队正式契约。PRD §21 / §48。

### POST /api/internal/meetings/{meeting_id}/transcript-events

Request（Transcript Event Contract，PRD §15）：

```json
{
  "event_id": "evt_001",
  "meeting_id": "meeting_001",
  "utterance_id": "utt_001",
  "segment_seq": 1,
  "speaker_id": "speaker_01",
  "text": "我们今天讨论会议助手 MVP。",
  "status": "FINAL",
  "start_ms": 1000,
  "end_ms": 3200,
  "emitted_at": 1789551600123
}
```

字段规则（PRD §16-§20）：

| 字段 | 类型 | 说明 |
|---|---|---|
| event_id | str | 全局唯一（PRD §16） |
| meeting_id | str | 必须与 path meeting_id 一致 |
| utterance_id | str | 同一段发言稳定，同一 meeting 内用于 PARTIAL 原地更新 |
| segment_seq | int? | FINAL 必须；PARTIAL 可缺省；meeting 内唯一且递增 |
| speaker_id | str | 允许 `speaker_N` / `unknown` |
| text | str | 识别文本 |
| status | `"PARTIAL"` \| `"FINAL"` | 二选一 |
| start_ms / end_ms | int? | 毫秒偏移 |
| emitted_at | int? | 事件时间（epoch ms，PRD §15） |

状态码：

- `200` — 接收成功（PARTIAL 已入 cache；FINAL 已持久化 / 幂等命中）
- `400` — payload 校验失败，或 `payload.meeting_id != path meeting_id`
- `404` — meeting 不存在
- `409` — meeting 状态不允许接收（CREATED / COMPLETED）；或 FINAL 幂等冲突；
  或 `event_id` 已存在（全局唯一）
  - `409` 绝不是 silent overwrite。

### Meeting 状态约束（arch §4）

```text
允许接收：IN_PROGRESS, FINALIZING
拒绝（409）：CREATED, COMPLETED   ← COMPLETED 后不得 silent append
```

### PARTIAL 语义

- 只进进程内 `live_utterance_cache`（key = meeting_id + utterance_id），**不写库**。
- 同一 utterance 的后续 PARTIAL 覆盖旧值（PRD §22）。
- 收到该 utterance 的 FINAL 后，cache 条目删除（PRD §23，Part 8）。

### FINAL 语义（PRD §23-§24）

```
Receive → Validate(status/meeting/seq) → Idempotency Check → Persist → Commit → (未来 SSE)
```

- 幂等（Case A）：完全相同（event_id 一致且 text/speaker/timing 相同）→ `200`，不重复 INSERT。
- 冲突（Case B）：同 `event_id`、内容不同 → **409**。
- 幂等兼容（Case C）：同 `(meeting_id, segment_seq)`、内容相同（不同 event_id）→ `200`；
  内容不同（不同 event_id）→ **409**。绝不产生两个正式 segment。
- `UNIQUE(event_id)`（全局，PRD §16）；`UNIQUE(meeting_id, segment_seq)`。
- 必须先持久化成功（Commit），未来才能广播 SSE（ADR D4）。

---

## 3. 领域异常 → HTTP 映射

| 领域异常 | HTTP |
|---|---|
| MeetingNotFoundError | 404 |
| TranscriptValidationError | 400 |
| InvalidMeetingStateTransition | 409 |
| TranscriptConflictError | 409 |

---

## 4. 尚未实现（后续 Step）

- `GET /api/meetings/{meeting_id}/events`（SSE）
- `GET /api/meetings/{meeting_id}/summary`
- `POST /api/meetings/{meeting_id}/summary/regenerate`
- `POST /api/internal/meetings/{meeting_id}/stream-end`
