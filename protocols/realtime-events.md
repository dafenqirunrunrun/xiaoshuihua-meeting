# 实时事件协议（Realtime Events / SSE）

> 状态：**✅ FROZEN**（ASR-0B，2026-09-19）
> 适用：后端 → 前端/任何消费者
> 对应实现：`backend/app/realtime/`、`frontend/src/utils/sse.ts`

---

## 1. 传输

```text
GET /api/meetings/{meeting_id}/events?after_segment_seq={N}
```

- `Content-Type: text/event-stream`；`Cache-Control: no-cache`；
  `Connection: keep-alive`；`X-Accel-Buffering: no`。
- 单 GET 长连接，**非 WebSocket**。

---

## 2. 恢复模型 ✅ FROZEN

**SSE 不是事实源，只是增量通知通道。** 固定恢复顺序：

```text
GET Meeting（会议状态）
  → GET Transcript Snapshot（已持久化 FINAL，取 max_segment_seq = N）
  → GET /events?after_segment_seq=N（先 catch-up 重放 > N 的 FINAL，再进入 live）
```

- `after_segment_seq` 是**唯一**恢复游标。`Last-Event-ID` **未实现**。
- `after_segment_seq < 0` → **400**；meeting 不存在 → **404**（优先于游标校验）。
- **Gap-free**：`connect()` 在 per-meeting 锁内**先读 catch-up、后注册订阅**，
  与 FINAL ingest 同锁互斥 → 无丢失窗口、无 connect race 双投。
- **非端到端 exactly-once**：消费者用稳定 id `final:<meeting_id>:<segment_seq>` 去重。

---

## 3. 帧格式 ✅ FROZEN

```text
id: <可选稳定 id>
event: <event_type>
data: {"sse_id":…, "event_type":…, "meeting_id":…, "emitted_at":…, "payload":{…}}

```

envelope：

```json
{
  "sse_id": "final:meeting_001:3",
  "event_type": "transcript.final",
  "meeting_id": "meeting_001",
  "emitted_at": 1789551600123,
  "payload": { }
}
```

### ⚠️ 具名帧（曾经的静默失效点）✅ FROZEN

每帧都带 `event: <event_type>`，属于**具名帧（named event）**。
按 SSE 规范，**具名帧不触发 `onmessage`**——只监听 `message` 的消费者会**静默收到 0 个事件**。

消费者**必须**逐事件名订阅：

```ts
es.addEventListener('<event_type>', handler)
```

前端统一封装在 `frontend/src/utils/sse.ts` 的 `subscribeSseEnvelopes()`。

---

## 4. 事件类型 ✅ FROZEN

| `event_type` | 稳定性 | payload | 稳定 id |
| --- | --- | --- | --- |
| `transcript.partial` | **非 durable** | 实时字幕片段 | — |
| `transcript.final` | **durable** | 正式 segment | `final:<meeting_id>:<segment_seq>` |
| `meeting.status` | durable | 会议状态 | — |
| `heartbeat` | — | 空 | — |
| `summary.started` | **非 durable** | `{version,status}` | `summary:<mid>:<version>:started` |
| `summary.completed` | **非 durable** | `{version,status}` | `summary:<mid>:<version>:completed` |
| `summary.failed` | **非 durable** | `{version,status}` | `summary:<mid>:<version>:failed` |

### 硬约束 ✅ FROZEN

- **PARTIAL 不回放**：只 live 推送，断线后不补（它从来不是事实源）。
- **`summary.*` 不回放**：`after_segment_seq` 不重放它们；重连后用 `GET /summary` 恢复。
- **payload 绝不含**：`raw_model_output`、堆栈、API key、prompt。
  仅 `{version, status}`。
- **`TRANSCRIPT_STREAM_END` 不广播**（PRD §30 未列出；契约不可自行扩展）。
- 心跳：`sse_heartbeat_interval`（默认 15s）空闲周期发 `heartbeat`。
- 顺序：`summary.started → summary.completed|failed → meeting.status(COMPLETED)`。

---

## 5. Broker 语义 ✅ FROZEN

- **进程内** broker（非 durable log）。重启丢订阅者/队列；靠 Snapshot + catch-up 恢复。
- 慢消费者队列满 → **驱逐**，**绝不阻塞 Producer**。
- **Commit-before-publish**：FINAL 必须先持久化成功才广播；SSE 广播失败
  （无订阅者 / 队列满 / 断开）**不回滚已提交事务**。

---

## 6. `TRANSCRIPT_STREAM_END` 终止契约 ✅ FROZEN（ASR-0B 决策）

> 这是本 Step 解决的两个 PENDING 之一。

### 6.1 端点与 payload

```text
POST /api/internal/meetings/{meeting_id}/stream-end
```

```json
{
  "event_id": "evt_end_001",
  "meeting_id": "meeting_001",
  "event_type": "TRANSCRIPT_STREAM_END",
  "last_segment_seq": 128
}
```

### 6.2 `last_segment_seq` 归属 ✅ FROZEN

**`segment_seq` / 会议内排序**由 Transcript Domain 拥有（见 transcript-format §4）。
因此：

- 上游**不要求**反向查询后端生成的排序。
- `last_segment_seq` **可选**：
  - **省略** → 后端从已持久化数据派生终值 `max(segment_seq) or 0`。
  - **提供** → 保留完整的 gap / overrun 校验能力（兼容与额外一致性检查）。
- **空会议**（无任何 FINAL）以 `last_segment_seq = 0` **正常 freeze**，这是合法终值。

> ⚠️ 这是对早期 PRD §25/§28 的一处**有意修正**：原文假设"至少有一个 segment"且
> `last_segment_seq` 必填，会导致零 segment 会议永久卡在 `FINALIZING`。
> 修正后 `ge=1 → ge=0` 且字段可选。

### 6.3 状态约束

| Meeting 状态 | 首次 STREAM_END |
| --- | --- |
| CREATED | **409** |
| IN_PROGRESS | **409** |
| FINALIZING | 正常处理 |
| COMPLETED | 仅幂等重试（相同终值）返回 200 |

### 6.4 幂等与完整性

- 相同终值重发 → `200`，不改变首次 `received_at`，不重复后续动作（**绝不重跑 LLM**）。
- 已接受 `last=3` 又发 `last=4` → **409**（一个 stream 只有一个终点）。
- 完整性：DB FINAL seq 集合必须 == `{1..last_segment_seq}`。
  缺口 → **409** `missing segments: [...]`；`max(DB) > last` → **409** overrun。
- STREAM_END 后再来新 FINAL → **409**（禁止 silent append）；
  已存在且内容完全相同的 FINAL 重试 → `200` 幂等。

### 6.5 ⏳ PENDING（未冻结，不得写死）

- **真实协作方的发送者身份与发送时机**（谁发、何时发、`event_id` 由谁生成）
  双方尚未确认。
- MVP 阶段使用的 stream-end 由**内部 mock** 发出，**仅供 E2E 回归**，
  代码中已标注"这不是最终协作契约"。
