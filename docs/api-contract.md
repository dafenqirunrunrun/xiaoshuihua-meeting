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

### GET /api/meetings — 会议列表（Step 8B-6，Meeting Discovery）

Query 参数（全部可选）：

- `status`：`CREATED | IN_PROGRESS | FINALIZING | COMPLETED`（四态之一；非法值 → **422**）。
- `limit`：`1..100`，默认 `20`（`LIST_DEFAULT_LIMIT`），最大 `100`（`LIST_MAX_LIMIT`）；越界 → 422。
- `offset`：`>=0`，默认 `0`；负数 → 422。

排序：`updated_at DESC, created_at DESC`（最近更新的在前）。不带 `status` 返回全部四态。

Response `200 / MeetingListOut`（复用 `MeetingOut`，无新增派生字段）：

```json
{
  "items": [
    {
      "id": "meeting_abc123",
      "title": "小水华 MVP 方案讨论",
      "description": null,
      "status": "IN_PROGRESS",
      "started_at": "2026-09-17T02:04:34",
      "ended_at": null,
      "completed_at": null,
      "created_at": "2026-09-17T02:04:34",
      "updated_at": "2026-09-17T02:04:34"
    }
  ],
  "total": 3,
  "limit": 20,
  "offset": 0
}
```

- `total` 为满足过滤条件的总条数（不计 limit/offset）。
- 路由顺序：`GET /api/meetings` 必须先于 `GET /api/meetings/{meeting_id}` 注册。
- 语义：Meeting Discovery / Resume 的**唯一**入口；`POST /resume` 不存在（见 architecture §13）。

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

### GET /api/meetings/{meeting_id}/state — Meeting State（Step UI-INTEL-1）

> **产品侧唯一的 Meeting State 读取入口**（与 §3 的 `/api/internal/...` 调试接口是
> 两个东西：那条返回完整 `MeetingState`、含 `model_name`，**前端不使用**）。

返回 AI 在会议进行中理解出的**结构化工作记忆**（`MeetingStatePublic`）。
**不是**会后 Final Summary —— 两者是独立数据域，禁止互相推导。

```json
{
  "meeting_id": "meeting_abc123",
  "version": 3,
  "last_processed_segment_seq": 16,
  "status": "ACTIVE",
  "updated_at": "2026-09-20T10:11:12.345678",
  "current_topic": "上线时间安排",
  "topics":         [{ "title": "…", "summary": "…", "source_segment_seqs": [1, 2] }],
  "decisions":      [{ "text": "…", "source_segment_seqs": [1, 2] }],
  "action_items":   [{ "task": "…", "owner": "小王", "deadline": null,
                       "source_segment_seqs": [4, 5] }],
  "open_questions": [{ "text": "…", "source_segment_seqs": [7] }],
  "risks":          [{ "text": "…", "source_segment_seqs": [8] }]
}
```

- **`status`** 是 Meeting State 自身的处理状态
  （`EMPTY` / `ACTIVE` / `STALE` / `FAILED` / `FROZEN`），**不是** Meeting 四态机。
  两个维度独立：会议 `COMPLETED` + 状态 `FAILED` 是**合法组合**。
- **`version`** 单调递增，**只由后端在成功更新时推进**（失败不推进）。
- Meeting 不存在 → **404**。会议存在但**尚未处理任何 FINAL** → **200** +
  `version = 0` + `status = EMPTY`（最常见的早期状态，**不是错误**）。
- **`owner` / `deadline` 契约上可为 `null`**（转写里确实没说负责人 / 时间）。
  UI 的「待确认」纯属展示层 fallback，**绝不写回后端**。
- **绝不含**：`model_name`（provider 身份是工程事实，不是产品事实）、`last_error`、
  raw model output、prompt、provider credential、内部异常 / 堆栈。由投影层
  （`MeetingStatePublic`）**结构性**保证（两个模型上都**没有** `last_error` 字段）。
- **`state_json` 损坏时**：不 500、不回写 DB；在投影层降级为 `status = FAILED`，
  保留仍可安全读取的标量（`version` / 游标 / `updated_at`），内容置空。
  **绝不**返回 `ACTIVE + 空内容`（那等于告诉用户「AI 正常运行」却什么也没有）。
- **`source_segment_seqs`** 本步只接住数据；点击溯源跳转属 **UI-INTEL-2**。
- **实时通知**：`meeting.state.updated`（`GET /events` 同一连接）**只是失效通知**，
  payload 仅 `{version, status}`；前端收到后重新调用本接口。语义与 id 规则见
  `protocols/realtime-events.md §4`。

---

## 2. Internal Transcript Ingress（`/api/internal`）

> 供 ASR / Mock Transcript Producer 调用；是跨团队正式契约。PRD §21 / §48。
> **两个 ingress 并存（Step ASR-0A）**：`/transcript-events` 是**内部**契约
> （snake_case，`segment_seq` 由 Producer 提供，PRD §15 原文不变）；
> `/asr-events` 是**外部协作者**契约（camelCase，无 `segmentSeq`，由 Transcript
> Domain 分配顺序）。详见 arch §17。

### POST /api/internal/meetings/{meeting_id}/asr-events（外部 ASR 输入，ASR-0A）

> 标准化 ASR 输出的正式入口。**不接收 `segmentSeq`**——顺序是 Transcript Domain
> 的职责。适配与顺序分配见 arch §17。

Request（协作者 camelCase 格式）：

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

`type` 取值 `transcript.partial` | `transcript.final`（FINAL 用同格式，`speakerId`
通常已解析为 `>= 0`）。

| 字段 | 类型 | 规则 |
|---|---|---|
| type | str | `transcript.partial` \| `transcript.final` |
| meetingId | str | 必须与 path `meeting_id` 一致，否则 **400** |
| asrSessionId | str | 1..**48**（硬约束，见下）；派生 `utterance_id` 必须落在 64 字符界内 |
| sentenceId | int | ≥ 0；**session 内**句子编号，**不等于** `segment_seq` |
| speakerId | int | ≥ -1；**-1 = 说话人暂未知**，合法，映射为内部 `unknown` |
| startMs / endMs | int | ≥ 0 且 `endMs >= startMs`；**session-local 相对偏移** |
| text | str | ≤ 4000 |
| provider | str | 不透明；不得据此硬编码厂商逻辑 |

> **`asrSessionId` 上限 48 是契约硬约束（ASR-0B 决策 5）**：
> `utterance_id = f"{asrSessionId}:{sentenceId}"` 最坏长度 = 48 + 1 + 10 = 59 ≤ 64。
> 超过 → 422。该约束已同步暴露在 OpenAPI schema（`maxLength: 48`）。
> 理由：保持派生 identity **精确单射**，优于静默哈希/截断一个 identity。

> **时间轴语义（ASR-0B 决策 3）**：`startMs/endMs` 是**每个 ASR session 内的相对偏移**，
> **跨 session 不可比较**（重连后上游可能归零）。排序**永远只认 `segment_seq`**；
> 这两个字段只用于存储 / 回显 / 展示，**绝不参与排序或完整性判断**。MVP **不做 rebase**。
> ⏳ V0.2 known limitation：会议级全局时间轴重建需先持久化 session 元数据
> （当前 `provider`/`asrSessionId`/`sentenceId` **不落库**）。

**拒绝规则**：未知字段（如 `segmentSeq`）或 snake_case 写法 → **422**（`extra="forbid"`，
契约漂移必须大声失败）。

**派生（客户端不提供）**：

```text
utterance_id  = f"{asrSessionId}:{sentenceId}"   ← PARTIAL 原地覆盖 & PARTIAL→FINAL 交接
FINAL event_id = 确定性摘要(meetingId|asrSessionId|sentenceId|FINAL)
```

状态码：`200` 接收成功 / `422` payload 校验失败 / `400` meetingId 不一致 /
`404` meeting 不存在 / `409` 状态不允许或幂等冲突。

> **协作者专用文档（ASR-0B）**：`GET /api/internal/docs`（Swagger UI）与
> `GET /api/internal/openapi.json`，仅覆盖三个 internal ASR 端点，**从真实路由派生**
> （不手写、不会漂移）。主 `/docs` 仍描述全部端点；此为**附加**视图，无路由搬迁。

**`segment_seq`**：FINAL 由 Backend 在 meeting lock 内分配 `max + 1`；
幂等重试复用已存 seq，不消耗新号。见 arch §17.3。

### POST /api/internal/meetings/{meeting_id}/transcript-events（内部契约）

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
- **Utterance 关闭（ASR-0B 决策 4）**：一旦该 utterance 收到 FINAL，即视为 **closed**；
  此后同 `utterance_id` 的 PARTIAL → **409**，**不写入 live cache**。
  理由：若接受，前端会渲染一条永远无法收敛的"识别中"行（它等待的 FINAL 已到达，
  且不会再作为 live 帧重发）。遵循"契约漂移必须大声失败"原则。
  该不变式是**领域级**的，在 `/asr-events` 与 `/transcript-events` 两个 ingress 上都生效
  （`transcript_service._ensure_utterance_open`）。

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
- **客户端订阅注意（具名帧）**：每帧都带 `event: {event_type}`，属于**具名帧**。
  按 SSE 规范具名帧**不触发 `onmessage`**，必须
  `es.addEventListener('<event_type>', h)` 逐事件名订阅（前端统一封装在
  `frontend/src/utils/sse.ts` 的 `subscribeSseEnvelopes`）。
- **事件类型**（本 Step 产出）：`transcript.partial`、`transcript.final`、
  `meeting.status`、`heartbeat`。`summary.*`（started/completed/failed）在 Step 7B
  产出（见 §4 Step 状态；payload 仅 `{version,status}`，非 durable，不回放）。
  `meeting.state.updated` 在 **Step UI-INTEL-1** 产出（同上：payload 仅
  `{version,status}`，非 durable，不回放）。
- **稳定 SSE id（Part 13）**：FINAL = `final:<meeting_id>:<segment_seq>`。
  Meeting State 变更 = `state:<meeting_id>:<version>:<status>`（**必须带 status** ——
  FAILED 刻意不推进 version，仅凭 version 无法区分同 version 下的
  `ACTIVE@N` 与 `STALE@N`；见 `protocols/realtime-events.md §4`）。
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
> **ASR-0B 更新**：`last_segment_seq` 改为**可选**且 `ge=0`。

Request（PRD §25，`event_id` / `meeting_id` 必须）：

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
- **`last_segment_seq` 可选（ASR-0B 决策 1/2）**：
  - **省略** → 后端派生终值 `max(segment_seq) or 0`（`stream_service._resolve_terminal_seq`）。
  - **提供** → 保留完整 gap / overrun 校验（兼容与额外一致性检查能力）。
  - 范围 `ge=0`：**空会议**（无任何 FINAL）以 `last_segment_seq = 0` 正常 freeze。
  - 依据：`segment_seq` 是 Transcript-owned，上游不应被要求反向查询我方排序。
  - 修正背景：早期 `ge=1` + 必填使**零 segment 会议永久卡在 FINALIZING**
    （0 → 422；1 → 409 missing [1]），见 `protocols/realtime-events.md §6.2`。

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
- `400` — event_type 非法 / path≠payload meeting_id
- `404` — meeting 不存在
- `409` —
  - meeting 状态不允许（必须 FINALIZING；CREATED/IN_PROGRESS 拒绝）
  - Transcript 不完整，存在缺口（`missing segments: [2]`）
  - DB 已存在比 last 更大的 seq（overrun，禁止静默忽略）
  - 已经接受过一个不同的 `last_segment_seq`（一个 Stream 只有一个终点）

> ⏳ **PENDING（未冻结）**：真实协作方的 STREAM_END **发送者身份与发送时机**
> 双方尚未确认。MVP 阶段该信号由内部 mock 发出，**仅供 E2E 回归**，代码已标注
> "这不是最终协作契约"。见 `protocols/realtime-events.md §6.5`。

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

## 3. Meeting State Debug API（`/api/internal`，Step AGENT-1）

> **仅开发 / 调试用，非产品 UI 契约**；前端不调用（本 Step 零前端改动）。
> Agent 是 Transcript 的下游消费者：**没有** agent 相关的写入口，状态由 FINAL 提交
> 或 `POST /finish` 后台推进。详见 `architecture.md §19`。

### GET /api/internal/meetings/{meeting_id}/state

返回该会议的**当前工作记忆**（Meeting State），最新版本，非 append-only 历史。

- 从未处理过任何 FINAL → **200**，返回空状态（`version=0`、`status="EMPTY"`、
  各列表为空）。这是合法答案，不是 404。
- meeting 不存在 → **404**。

```json
{
  "meeting_id": "meeting_001",
  "version": 3,
  "last_processed_segment_seq": 6,
  "status": "ACTIVE",
  "model_name": "fake",
  "updated_at": "2026-09-19T10:00:00Z",
  "current_topic": "上线安排",
  "topics": [
    { "title": "上线安排", "summary": "讨论上线时间与稳定性", "source_segment_seqs": [1, 2] }
  ],
  "decisions": [
    { "text": "定下周三上线", "source_segment_seqs": [3] }
  ],
  "action_items": [
    { "task": "后端接口联调", "owner": "小王", "deadline": "周二之前", "source_segment_seqs": [4, 5] }
  ],
  "open_questions": [
    { "text": "支付接口稳定性需要确认", "source_segment_seqs": [6] }
  ],
  "risks": []
}
```

- `status` ∈ `EMPTY` / `ACTIVE` / `STALE` / `FAILED` / `FROZEN`（Agent 内部状态，
  **不是** Meeting 状态机的一部分 —— Meeting 仍然只有四态）。
- `version` 与 `last_processed_segment_seq` 由**系统维护**，模型不可写。
- `STALE` 表示有 last-good 内容但最近一次更新失败；`FAILED` 表示尚无可用内容。
  两者都不推进游标，已成功内容保持不变。

### POST /api/internal/meetings/{meeting_id}/state/process-pending

调度一次后台 drain（自己的 DB session，不阻塞请求），把 pending FINAL 折叠进状态。
**幂等**：无 pending → 后台报告 `NO_WORK`，零状态变化、零 LLM 调用。

Response **200**：

```json
{
  "meeting_id": "meeting_001",
  "scheduled": true,
  "version_before": 3,
  "last_processed_segment_seq": 6
}
```

- meeting 不存在 → **404**。
- `MEETING_AGENT_ENABLED=false` 时该调用为 no-op（后台直接返回 `DISABLED`），
  主链路行为与变更前完全一致。

---

## 4. 领域异常 → HTTP 映射

| 领域异常 | HTTP |
|---|---|
| MeetingNotFoundError | 404 |
| TranscriptValidationError | 400 |
| InvalidMeetingStateTransition | 409 |
| TranscriptConflictError | 409 |

---

## 5. Step 状态

> **Step AGENT-2 已完成**：Final Summary V2（Meeting State 作为纪要主输入）。
> **本步不新增 / 不修改任何 HTTP / SSE 契约** —— 这是刻意的设计结果，不是遗漏：
>
> - **`GET /api/meetings/{id}/summary` 的响应形状完全不变**（仍是
>   `SummaryOut`：overview / key_topics / conclusions / open_questions …）。
>   V2 的富结果（decisions / action_items / risks / evidence）**扁平化投影**回
>   既有字段（见 `architecture.md §20.7`），因此**前端零改动**。
> - **SSE `summary.*` 不变**（仍是 `{version, status}`）。
> - **Meeting 状态机不变**（四态，无新状态）。
> - **无 DB schema change / 无 migration**：V2 明细写在既有
>   `raw_model_output` 列内（Known Limitation，见 `architecture.md §20.8`）。
> - 新增的只有**内部**模块（`final_summary_service` / `final_summary_prompts` /
>   `final_summary_schemas` / `evidence`）与 settings，**不对外暴露**。
> - **`GET /api/internal/meetings/{id}/state`（§3）不变**，仍是 AGENT-1 的形状。
>
> 影响到的既有行为：`finalize_for_meeting` 现在会**先同步强制 flush Meeting State**
> 再生成纪要（§2 排序不变式）—— 见 `architecture.md §20.2`。这改变的是**触发时序**，
> 不改变任何对外契约。

> **Step AGENT-1 已完成**：Meeting State Agent（会议工作记忆）。新增
> `GET/POST /api/internal/meetings/{id}/state[/process-pending]`（调试用，见 §3）；
> FINAL 首次提交与 `POST /finish` 会后台推进状态；新增 1 张表（`meeting_state`，
> 无 migration）；Meeting 四态机 / Freeze 语义 / Transcript 契约 / LLM Provider /
> 前端**零改动**。见 `architecture.md §19`。

> **Step UI-INTEL-1 已完成**：Meeting Intelligence Panel。本步新增 / 变更的契约：
>
> - **`GET /api/meetings/{meeting_id}/state`** —— **产品侧唯一**的 Meeting State
>   读取入口（`MeetingStatePublic`，见 §1）。会议不存在 → **404**；
>   尚未处理任何 FINAL → **200** + `version=0` + `status=EMPTY`（不是错误）。
>   `state_json` 损坏 → 投影层降级为 `FAILED`（不 500、不回写 DB）。
>   internal 调试接口 `/api/internal/...` **保留且不变**，前端不使用。
> - **SSE `meeting.state.updated`**（`GET /events` 同一连接）：
>   payload 仅 `{version, status}`；稳定 id `state:<mid>:<version>:<status>`
>   （**必须带 status** —— FAILED 不推进 version，见 `realtime-events.md §4`）；
>   **非 durable**，不回放，不改变 `after_segment_seq` 恢复协议；
>   **先 commit 后 publish**，发布失败不回滚状态。
> - **零改动**：Meeting 四态机 / Freeze 语义 / Transcript 契约 / ASR 契约 /
>   LLM Provider / AGENT-1 核心 schema / **DB schema（无 migration）**。
>   见 `architecture.md §21`。

> Step 6 已完成：Transcript Freeze（durable 边界）+ Finalization Readiness
> （service 层派生，无公开 API）—— 见 `architecture.md §7.0/§7.3` 与 `ADR-001 D13`。

> Step 7A 已完成：LLMProvider + DeepSeekProvider + Summary Generation Pipeline
> （`summary_service.generate`：Freeze → LLM → Schema 校验 → Summary 持久化）。
> 均**已实现**：service 层内部生成、无 HTTP 端点 / 无 SSE `summary.*`、Meeting 保持
> FINALIZING —— 见 `architecture.md §9` 与 `ADR-001 D14`。

> **Step 7B 已完成**：Finalizer + Summary API + Summary SSE + Meeting Completion。
> 本步新增 / 变更的契约：
>
> - **`GET /api/meetings/{meeting_id}/summary`** —— 最新版本 Summary 快照
>   （`SummaryOut`：id / meeting_id / version / status / overview / key_topics /
>   conclusions / open_questions / model_name / raw_model_output / created_at）。
>   - `meeting` 不存在 → **404 "meeting not found"**；存在但无 summary → **404
>     "summary not found"**；有（GENERATING / COMPLETED / FAILED）→ **200**。
>   - 是前端重连的 **durable 恢复事实源**（Summary SSE **非 durable、不回放**）。
> - **`POST /api/meetings/{meeting_id}/summary/regenerate`** → **202**
>   `{meeting_id, version, status: "GENERATING"}`（不阻塞 LLM）。
>   - 同步分配 vN+1 + 持久化 GENERATING 行 + 发 `summary.started`，然后
>     BackgroundTasks（自己的 DB session）后台跑 Phase B/C。
>   - meeting 必须 FINALIZING **或 COMPLETED**（regenerate 永不把 Meeting 移出
>     COMPLETED）；CREATED / IN_PROGRESS → **409**。
>   - latest 已是 GENERATING → **409**（避免重复模型成本）。
>   - 版本只 **append**（v1→v2→v3），终态不覆盖；输入仅持久化 FINAL ASC
>     （`is_frozen` 门控，COMPLETED 会议同样满足 D6）。
> - **SSE `summary.*`**（`GET /events` 同一连接）：
>   - `summary.started` / `summary.completed` / `summary.failed`；
>   - payload 仅 `{version, status}`（**绝不含** raw_model_output / 堆栈 / API key / prompt）；
>   - 稳定 id `summary:<meeting_id>:<version>:<started|completed|failed>`；
>   - **非 durable**：不随 `after_segment_seq` 回放，重连用 GET /summary 恢复；
>   - 顺序：`summary.started → summary.completed|failed → meeting.status(COMPLETED)`。
> - **首个 accepted STREAM_END auto-triggers Finalizer**（BackgroundTasks，进程内
>   MVP 触发，≠ durable job queue）；相同 STREAM_END retry → `newly_accepted=False`，
>   **绝不**重跑 LLM（见 `architecture.md §11` 与 `ADR-001 D15`）。
> - **Meeting 状态**：Summary 终态（COMPLETED / FAILED）**之后**才
>   FINALIZING → COMPLETED（`complete_after_summary`，唯一的完成路径）；
>   无 Meeting FAILED / FINALIZING_FAILED。
> - **canonical FINAL retry 跨 COMPLETED 边界仍 200**（幂等重发不是 append；
>   新 FINAL / PARTIAL 在 COMPLETED 仍 409）——见 §Transcript 幂等。

> 仍留待后续：Mock Producer 的 `--phase` 分阶段能力（当前单次全流）。
