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
Receive → Validate(status/meeting/seq) → Idempotency Check → Persist → Commit → SSE Broadcast
```

- 幂等（Case A）：完全相同（event_id 一致且 text/speaker/timing 相同）→ `200`，不重复 INSERT。
- 冲突（Case B）：同 `event_id`、内容不同 → **409**。
- 幂等兼容（Case C）：同 `(meeting_id, segment_seq)`、内容相同（不同 event_id）→ `200`；
  内容不同（不同 event_id）→ **409**。绝不产生两个正式 segment。
- `UNIQUE(event_id)`（全局，PRD §16）；`UNIQUE(meeting_id, segment_seq)`。
- **Commit-before-publish（Step 5）**：必须先持久化成功（Commit）才广播 SSE
  （ADR D4 / arch §8.3）。SSE 广播失败（无订阅者 / 队列满 / 断开）不回滚已提交事务。

### GET /api/meetings/{meeting_id}/events — SSE Incremental Stream

> 实时增量流（PRD §30/§31，arch §8.3）。Step 5。单 GET 连接，非 WebSocket。

- 响应 `Content-Type: text/event-stream`；`Cache-Control: no-cache`、
  `Connection: keep-alive`、`X-Accel-Buffering: no`。
- Query 参数：`after_segment_seq`（int，默认 0）——恢复游标：重放 `segment_seq > 该值` 的
  持久化 FINAL（catch-up）后再进入 live。
- `after_segment_seq < 0` → **400**；Meeting 不存在 → **404**（优先于游标校验）。
- **恢复模型**（gap-free）：调用方先 `GET /transcript` 拿 `max_segment_seq = N`，
  再以 `after_segment_seq=N` 连接。connect 在 **per-meeting 锁内**先读持久化 FINAL
  （catch-up）、后注册订阅——与 FINAL ingest 同锁互斥，故无丢失 / 无重复窗口。
- **帧（envelope）**：`{sse_id?, event_type, meeting_id, emitted_at, payload}`；
  每帧包含可选的 `id:`、`event:`、`data:` 行 + 空行。
- **事件类型**（本 Step 产出）：`transcript.partial`、`transcript.final`、
  `meeting.status`、`heartbeat`。`summary.*` 仅保留（后续 Step）。
- **稳定 SSE id（Part 13）**：FINAL = `final:<meeting_id>:<segment_seq>`。
- **PARTIAL 不重放**（Part 12）：只 live 推送，断线后不补。
- `TRANSCRIPT_STREAM_END` **不广播**（PRD §30 未列出，契约不可扩展）。
- `Last-Event-ID` header **未实现**（Part 14）：`after_segment_seq` 是唯一恢复游标。
- 心跳：`settings.sse_heartbeat_interval`（默认 15s）。空闲连接周期性收到 `heartbeat`。
- Broker 进程内（Part 20）：重启丢订阅者/队列，靠 Snapshot + catch-up 恢复；
  multi-instance 后续演进（Redis / Kafka）。

```text
恢复示例（after_segment_seq=1，DB 已有 1,2,3）：
  event: transcript.final  seq=2        ← catch-up（segment_seq ASC）
  event: transcript.final  seq=3        ← catch-up
  event: transcript.partial / final …   ← 之后全部为 live
```

### POST /api/internal/meetings/{meeting_id}/stream-end

> 终端 STREAM_END；**持久化落盘**（`transcript_stream` 表），重启不丢失。Step 4。

Request（PRD §25，event_id/meeting_id 必须）：

```json
{
  "event_id": "evt_end_001",
  "meeting_id": "meeting_001",
  "event_type": "TRANSCRIPT_STREAM_END",
  "last_segment_seq": 128
}
```

- `event_type` 严格要求为 `"TRANSCRIPT_STREAM_END"`（常量），否则 400。
- `path meeting_id == payload meeting_id`，否则 400（§49.1 精神）。

Response `200 / StreamEndOut`：

```json
{
  "meeting_id": "meeting_001",
  "last_segment_seq": 128,
  "transcript_complete": true
}
```

状态码：

- `200` — 首次接受（已落盘 & 完整性通过），或幂等命中
- `400` — event_type 非法 / last_segment_seq 缺失 / path≠payload meeting_id
- `404` — meeting 不存在
- `409` —
  - meeting 状态不允许（必须 FINALIZING；CREATED/IN_PROGRESS 拒绝）
  - Transcript 不完整，存在缺口（`missing segments: [2]`）
  - DB 已存在比 last 更大的 seq（overrun，禁止静默忽略）
  - 已经接受过一个不同的 `last_segment_seq`（一个 Stream 只有一个终点）

状态约束（Part 4）：

| 状态 | STREAM_END |
|---|---|
| CREATED | 409 |
| IN_PROGRESS | 409 |
| FINALIZING | 正常处理（首个） |
| COMPLETED | 仅幂等 retry（相同 last）返回 200 |

幂等（Part 7）：

- 相同 `last_segment_seq` 已接受 → 再次发送同值 → `200`，不改变首次 `received_at`，不重复后续动作。
- 已接受 `last=3` 又发 `last=4` → `409`。

完整性检查算法（Part 6）：

```text
DB FINAL seq 集合  必须 == {1,2,...,last_segment_seq}
缺口（如 [1,2,4,5] & last=5）→ 409 missing=[3]
max(DB) > last（如 [1,2,3,4] & last=3）→ 409 overrun
```

FINAL after Stream End（Part 8）：

- 已存在且内容完全相同的 FINAL retry → `200` 幂等，不增 DB。
- 新 FINAL（seq 不在 DB）→ `409`，禁止 silent append。

### FINAL 与 STREAM_END 同步边界（Part 9）

- `POST transcript-events`（PARTIAL/FINAL）和 `POST stream-end` 对同一 meeting
  共享同一个进程内 per-meeting `RLock`（`app/services/lock_registry.py`）。
- 保证：FINAL 写入与完整性检查互斥；STREAM_END 成功后再不会并发写入新 FINAL。
- **单进程 MVP 才有效**；多进程/多实例/PostgreSQL 需升级为数据库级并发控制（ADR D8）。

### 持久化（Part 5）

- `transcript_stream` 表：`meeting_id (PK), last_segment_seq, status, received_at`。
- 重启后再次发送相同 STREAM_END → 仍 `200 幂等`（Case 12 实证）。

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

- `GET /api/meetings/{meeting_id}/summary`
- `POST /api/meetings/{meeting_id}/summary/regenerate`
- Summary Pipeline / LLM / FINALIZING → COMPLETED（Step 7：`summary.status` 进入终态
  COMPLETED 或 FAILED 后驱动 `meeting.status = COMPLETED`）
- Mock Producer 的 `--phase` 分阶段能力（当前单次全流）

> Step 6 已完成：Transcript Freeze（durable 边界）+ Finalization Readiness
> （service 层派生，无公开 API）—— 见 `architecture.md §7.0/§7.3` 与 `ADR-001 D13`。

> Step 7A 已完成：LLMProvider + DeepSeekProvider + Summary Generation Pipeline
> （`summary_service.generate`：Freeze → LLM → Schema 校验 → Summary 持久化）。
> 均为 **service 层内部生成**，**无新增 HTTP 端点**、无 SSE `summary.*`、
> Meeting 保持 FINALIZING —— 见 `architecture.md §9` 与 `ADR-001 D14`。
> 前端读取（GET /summary）与重新生成（POST regenerate）留待 Step 7B。
