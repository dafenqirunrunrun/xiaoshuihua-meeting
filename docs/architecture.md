# 小水华会议助手 MVP V0.1 架构说明

> 依据：`prd.md` + `fronted_designer.md` + `docs/decisions/ADR-001-mvp-architecture.md`
> 本文档与 Accepted ADR 冲突时，以 ADR / 本架构为准（ADR 优先）。
> 版本：MVP V0.1（配合 Step 3 落盘）

---

## 1. 系统边界

```text
┌──────────────── 上游（语音系统） ────────────────┐
│ Audio                                          │
│   ↓                                            │
│ ASR Service        （当前阶段：Mock Transcript  │
│   ↓                  Producer 代替）            │
│ Transcript Event                               │
└──────────────────────┬─────────────────────────┘
                       │ HTTP / Internal Contract
                       ▼
┌──────────────── 当前系统（FastAPI） ─────────────┐
│  Transcript Ingress                             │
│   ├─ live_utterance_cache   (PARTIAL, 进程内)   │
│   ├─ transcript_segment     (FINAL, 持久化)     │
│  Meeting State Machine                         │
│  SSE Broadcast              （后续 Step）       │
│  Summary Pipeline           （后续 Step）       │
└──────────────────────┬──────────────────────────┘
                       │ Snapshot + SSE
                       ▼
                 React Frontend
                   （Consumer / Observer）
```

## 2. 核心原则

```text
Backend   = Meeting State + Transcript 的 Source of Truth
Frontend  = 纯 Consumer / Observer
```

- Frontend 断网 / 刷新 / 关闭浏览器 / SSE 断开，均不得影响 Backend：
  继续接收 Transcript、继续持久化 FINAL、维护 Meeting State、后续执行 Finalizer。
- Transcript 是会议唯一事实源；Summary 是 AI 派生结果，可重新生成，不反向改动 Transcript。

## 3. Meeting 状态机（4 态，禁止扩展）

```text
CREATED
   ↓ start
IN_PROGRESS
   ↓ finish
FINALIZING
   ↓ finalizer 完成
COMPLETED
```

- 禁止新增 `FINALIZING_FAILED`、`FAILED` 等 Meeting 状态。
- Summary 失败表达：`meeting.status = COMPLETED` + `summary.status = FAILED`。
- 前端展示"会议记录已保存 + 纪要生成失败 + [重新生成]"，禁止显示"会议失败"。

## 4. ASR 边界

当前阶段**不接真实 ASR**。

- ASR 是系统外部的上游 Producer，语音系统负责：麦克风、音频采集、PCM、采样率、
  音频编码、ASR 模型、真实 ASR WebSocket/协议。
- Meeting Backend 不处理以上任何项，只定义并实现 **Transcript Event 接收契约**。
- 当前开发用 **Mock Transcript Producer** 模拟 PARTIAL / FINAL / TRANSCRIPT_STREAM_END。
- 未来真实 ASR 只需按契约调用内部 Transcript API 即可接入。

## 5. Transcript 原则

### 5.1 PARTIAL

```text
临时数据、允许覆盖、不进入正式 transcript_segment 表
MVP：进程内 live_utterance_cache（key: meeting_id + utterance_id）
后续可替换 Redis（跨实例）
```

**生命周期语义：`Backend Process Scoped Temporary State`**

```text
与 Frontend / Browser Session / SSE Connection 生命周期完全解耦
```

- 即使当前没有任何 SSE 连接，Backend 依然可继续接收 PARTIAL、更新 cache、接收 FINAL、持久化 FINAL。
- cache 只为 Backend 自己服务：临时保存最新 PARTIAL，供 SSE 实时推送给订阅者时使用——它不依赖任何下游连接存在。
- 进程重启后 PARTIAL 丢失可接受：PARTIAL 不是 Source of Truth（ADR D3/D4）。

规则：

- 同一 utterance 的后续 PARTIAL 覆盖旧值，禁止无限 append。
- 收到对应 utterance 的 FINAL 后，删除该 cache 条目（PRD §23，Step 3 Part 8）。

### 5.2 FINAL

固定处理顺序（禁止先广播后落库）：

```text
Receive
 ↓ Validate
 ↓ Idempotency Check
 ↓ Persist
 ↓ Commit Success
 ↓ SSE Broadcast（Step 5：commit-before-publish，见 §8.3）
```

- FINAL 必须持久化成功后再广播（SSE 广播失败不得回滚已提交事务，见 §8.3 / ADR D12）。
- 数据库约束：`UNIQUE(event_id)`、`UNIQUE(meeting_id, segment_seq)`。

### 5.3 幂等与冲突

- 完全相同 FINAL 重发 → 幂等成功，不重复 INSERT。
- 同 event_id、内容不同 → 409（禁止 silent overwrite）。
- 同 (meeting_id, segment_seq)、内容相同（event_id 不同）→ 幂等成功。
- 同 (meeting_id, segment_seq)、内容不同 → 409。

## 6. Frontend 恢复模型

```text
GET Meeting
 ↓
GET Transcript Snapshot      ← 正式 FINAL 全部恢复
 ↓
Subscribe SSE                ← 实时增量，SSE 非事实源
 ↓
Receive incremental events
```

- SSE 只是实时增量通知机制；页面刷新 / 重连 / 重新进入一律用 Snapshot 恢复后去重。

## 7. Stream End 与 Freeze

- 用户 Finish 进入 FINALIZING **不代表 Transcript 已完整**。
- 必须等待后续：FINAL segments → `TRANSCRIPT_STREAM_END`（含 `last_segment_seq`）→
  完整性确认（最后已保存 seq == last_segment_seq，且无显式缺口）→ Freeze。
- **禁止** `sleep(3)` 等固定等待猜测 ASR 已结束。
- Freeze 后不允许 silent append；COMPLETED 后收到 Transcript → 409。

### 7.0 Transcript Freeze = durable 边界（Step 6 已实现）

**freeze 边界即 `transcript_stream` 中被 ACCEPTED 的行**：

```text
receive STREAM_END
 ↓ validate meeting / status / event_type / meeting_id
 ↓ integrity check（gaps / overrun）
 ↓ persist transcript_stream（status=ACCEPTED, last_segment_seq）  ← durable freeze boundary
 ↓ commit success
 ↓ clear 该 meeting 的 transient PARTIAL cache
```

- **不新增** `frozen` / `finalization_ready` / `FROZEN` 字段或状态；`ACCEPTED` 不重命名。
  未做 schema change（ADR D1/D12 精神：不扩四态、不引入冗余状态）。
- **双维度 ingest 门控（Step 6）**：
  - `meeting.status` 控制 **Meeting 生命周期阶段**（CREATED/COMPLETED → 409）；
  - `transcript_stream ACCEPTED` 控制 **Transcript 是否已冻结** —— 职责分离、非重复状态。
- **Freeze 后 PARTIAL → 409**（在进入 `live_utterance_cache` 之前检查，且不得修改 cache）。
- **Freeze 后新 FINAL → 409**；**Freeze 后 canonical existing FINAL retry → 200 幂等**
  （先按 event_id / segment_seq 命中已有行做 canonical 比较，仅对"新 FINAL"拒绝）。
- **PARTIAL cache 清理时序**：只在 freeze **commit 成功之后** `clear_meeting`；
  完整性检查失败（gap / overrun → 409）**不得**清理 cache（Transcript 尚未冻结，
  residual PARTIAL 需保留给重试路径）。cache 清理只是**临时状态清理**，
  不是 freeze 的 Source of Truth（freeze 的 Source of Truth 是 `transcript_stream` 行）。

### 7.1 Finalization Readiness（Step 6，派生状态，不新增持久化字段）

- `stream_service.get_finalization_readiness(db, meeting_id)`（及 `is_finalization_ready` 布尔形式）
  为未来 Step 7 Finalizer 提供领域门控。
- Ready 条件（全部可从 DB 派生）：
  - meeting 存在；
  - `meeting.status == FINALIZING`；
  - `transcript_stream` 存在 ACCEPTED 行；
  - `last_segment_seq` 已持久化。
- 因为 ACCEPTED 行已隐含完整性检查通过，readiness 不做重复全量 gap scan。
- **不新增公开 API**（如 `GET /finalization-ready`）；Step 6 只在 service 层提供。
- **Meeting 状态保持 FINALIZING**：即便 STREAM_END accepted / Transcript frozen /
  finalization_ready=true，也**绝不在 Step 6 变 COMPLETED**（COMPLETED 由 Step 7
  Summary Pipeline 完成后驱动）。

### 7.2 STREAM_END 持久化（Step 4）

- STREAM_END 不能只存内存：重启会遗忘。落盘到 `transcript_stream` 表
  （`meeting_id PK, last_segment_seq, status, received_at`）。
- 幂等：同值重发 → 200 不改变首选 `received_at`；异值 → 409（一个 Stream 一个终点）。
- 状态门：仅 `FINALIZING` 接受首个 STREAM_END；CREATED/IN_PROGRESS → 409；
  COMPLETED 仅幂等 retry。
- 完整性：FINAL seq 必须恰为 `1..last_segment_seq`；显式缺口 → 409 `missing=[...]`；
  `max(seq) > last` → 409（overrun，不得静默忽略）。

### 7.3 FINAL 与 STREAM_END 共享同步边界（Step 4）

```text
FINAL ingest  +  STREAM_END integrity check
       二者共用同一个 per-meeting RLock（lock_registry.meeting_lock）
```

- 避免：FINAL 写入与完整性检查并发竞态、STREAM_END 成功后再并发写新 FINAL。
- 单进程 MVP 有效；多进程/多实例必须升级为 DB 级并发控制（ADR D8）。

## 8. 分层与并发

### 8.1 分层

```text
api/routes      → HTTP / Pydantic
services        → 状态校验 / 幂等 / 业务规则
repositories    → SQLAlchemy Query / Insert
```

- Service 不 import `HTTPException`；领域层抛领域异常，由 `main.py` 全局 handler 映射：
  - `MeetingNotFoundError` → 404
  - `TranscriptValidationError` → 400
  - `InvalidMeetingStateTransition` / `InvalidTranscriptStateError` / `TranscriptConflictError` → 409

### 8.2 并发（MVP 与本项目的明确限制）

- 当前：`threading.RLock`（进程内）+ SQLite busy timeout，仅适用 **single-process MVP**。
- 若未来 `uvicorn --workers > 1` / 多实例部署 / PostgreSQL，必须升级为数据库级并发控制：
  `SELECT ... FOR UPDATE`（或 FOR NO KEY UPDATE）、conditional UPDATE、
  optimistic locking / version column。
- 不能用当前 RLock 作为生产级跨进程锁；也不要做超出 MVP 的过度设计。

### 8.3 SSE Incremental Stream（Step 5 已实现）

**端点**：`GET /api/meetings/{meeting_id}/events`（StreamingResponse，`text/event-stream`）。

**信封**：`{event_type, meeting_id, emitted_at, payload}`；
本 Step 产出 `transcript.partial` / `transcript.final` / `meeting.status` / `heartbeat`；
`summary.*` 仅保留类型（后续 Step 用）。`TRANSCRIPT_STREAM_END` **不广播**
（PRD §30 未列出，契约不可扩展）。

**Commit-before-publish（核心不变式）**：

```text
FINAL: Persist → Commit Success → Publish SSE
```

- SSE publish 失败（无订阅者 / 队列满 / 断开）**绝不能回滚**已成功的业务事务。
- Producer 线程 `publish()` 永不阻塞：只做 bounded `put_nowait`（经
  `loop.call_soon_threadsafe` 调度到订阅者事件循环），队列满即**驱逐慢消费者**。

**Gap-free recovery（connect 在 per-meeting lock 内原子完成）**：

```text
GET Transcript Snapshot        → max_segment_seq = N
GET /events?after_segment_seq=N
  connect()  [under meeting_lock]
    1. read persisted FINAL with seq > N      (catch-up 源)
    2. register SSE subscriber                (同一把锁内)
  event_stream()
    1. yield catch-up envelopes（segment_seq ASC）
    2. 然后进入 live 有界队列
```

- `after_segment_seq`：恢复游标；`<0` → 400；`> 当前 max` → catch-up 为空、live 继续。
- 因为 FINAL ingest 与 SSE connect 使用**同一把 per-meeting lock**，catch-up 查询与
  subscriber 注册之间不存在 FINAL 提交窗口，因此 **Snapshot → Catch-up → Live 的服务端
  handoff 不会丢失 FINAL**，也不会因该 connect race 产生 catch-up/live 双投。
- 注意：这不是端到端 exactly-once delivery。网络断开、客户端游标未及时推进等情况下仍可能
  发生重放；**Consumer 应使用稳定 FINAL SSE id `final:<meeting_id>:<segment_seq>` 或
  `segment_seq` 做幂等去重**（Part 13）。
- **PARTIAL 不重放**：只 live 推送，断线不补（恢复靠 Snapshot + persisted FINAL catch-up）。
- **稳定 SSE id**（Part 13）：FINAL 为 `final:<meeting_id>:<segment_seq>`。
- `Last-Event-ID` header：**未实现**（Part 14），`after_segment_seq` 是唯一恢复游标。

**Broker 生命周期（Part 20）**：

- EventBroker 为**进程内**对象（Broker ≠ durable event log；重启丢订阅者/队列，
  靠 Snapshot + 持久化 FINAL catch-up 恢复）。多实例 → Redis Pub/Sub / Streams / Kafka（后续）。
- 心跳：`settings.sse_heartbeat_interval`（默认 15s，可调小便于测试）。
- 断开清理：`event_stream` 的 `finally` 必然 `unsubscribe`；app shutdown 时 `disconnect_all()`。
- **慢消费者策略（adopted in ADR-001 D12）**：队列满 → 驱逐（断连 sentinel）而非阻塞 Producer

## 9. Summary Generation Pipeline（Step 7A 已实现）

> 契约：Freeze 后（FINALIZING + `transcript_stream ACCEPTED`）才可调 LLM。
> 本 Step 是 **service 层内部生成**（`summary_service.generate`）；无新增 HTTP 端点，
> 无 SSE `summary.*`，Meeting 保持 FINALIZING（COMPLETED 交给 Step 7B，summary 终态后驱动）。

### 9.1 LLM 边界（Provider 抽象，ADR D14）

- 业务层（`summary_service`）只依赖 `LLMProvider` 协议（`app/services/llm/base.py`）：
  `name: str` + `complete(prompt) -> str`。单原语；结构化校验是业务层（Pydantic）的事。
- 三个 provider：
  - `DeepSeekProvider`（`app/services/llm/deepseek.py`）：**env-only 配置**
    （`deepseek_base_url / deepseek_api_key / deepseek_model`），OpenAI 兼容
    `POST {base}/chat/completions`（`response_format={"type":"json_object"}`），httpx 同步。
    未配置 key → **lazy**：只在首次 `complete()` 抛 `SummaryGenerationError`，不阻断 import / startup。
    HTTP 超时 / 5xx / 结构异常 → `SummaryGenerationError`，不抛 500。
  - `FakeProvider`（`app/services/llm/fake.py`）：**默认测试基础设施**，离线确定性
    （scripted responses / error / repair 场景）。
  - `LLMProviderFactory`（`app/services/llm/factory.py`）：`settings.llm_provider`
    支持 `"fake"`（默认）与 `"deepseek"`；未识别值回落 Fake（绝不阻断启动）。
- **测试注入**：`summary_service.generate(..., provider=...)` 直接注入，无需改业务代码。

### 9.2 生成管线（Phase A / B / C，无锁调 LLM）

```text
Phase A [locking, 短事务]
  meeting 存在 (404)  &  status == FINALIZING (409)  &  transcripts frozen (D13, 409)
  &  同 version 已终态 (COMPLETED/FAILED) -> 幂等 no-op（PRD §44，终态不覆盖）
  -> upsert 行 status=GENERATING（崩溃后可 resume）
Phase B [NO lock, NO transaction —— LLM 调用全程无 DB 锁/事务]
  session 内 list_sorted(FINAL ASC)  ->  build prompt（<transcript> 边界）
  -> provider.complete()  <=>  parse_and_validate()（Pydantic）
Phase C [locking, 回写]
  重新取锁：并发已写终态 -> 409（不覆盖赢家）
  success -> status=COMPLETED + raw_model_output + model_name + version=1
  failure -> status=FAILED + reason（meeting 仍 FINALIZING）
```

- **不锁长事务**：A/B/C 三阶段，Phase B 不持任何 meeting 锁、不持任何 DB 事务。
  输入只从**持久化 FINAL**（`list_sorted ASC`）读取；PARTIAL 永不进 prompt。
- **无静默截断**：`MAX_TRANSCRIPT_CHARS`（默认 90_000 字符）硬上界——
  transcript 超限 → FAILED（reason=transcript too large），**绝不静默丢弃**。
- **有界重试**：`settings.summary_max_attempts`（默认 2）。结构化校验失败 →
  **repair prompt**（携带失败原文 + 具体 Pydantic 错误，重新喂同一 `<transcript>`
  素材）→ 重试（最多 attempts 次）。`summary_parse_repair=False` → 首次失败即 FAILED。
- **幂等 / 版本**：初始生成固定 `version=1`；`UNIQUE(meeting_id, version)`；
  同 version 已 COMPLETED / FAILED 的重复 generate → 幂等 no-op，**不覆盖**。
  显式重新生成（regenerate+新版本）在 Step 7B。
- **失败表达**：仅 `summary.status = FAILED` + reason；Meeting **保持 FINALIZING**
  （Step 7A 绝不驱动 COMPLETED）。`GENERATING` 中间态若进程崩溃 → resume 路径（Phase A）。
- **并发**：Phase A/C 用与 FINAL ingest / STREAM_END **同一把 per-meeting lock**
  （`lock_registry.meeting_lock`）；Phase B 完全锁外。单进程 MVP 有效（ADR D8）。

### 9.3 结构化输出（schemas/summary.py）

- `MeetingSummarySchema`（PRD §43）：`overview: str`、`key_topics / conclusions /
  open_questions: list[str]`，**Pydantic 校验即成功判据**。
- `raw_model_output` 存 LLM 原始文本（不回写覆盖）；`model_name` 存 provider 名。
- `json.dumps(items, ensure_ascii=False)` 入库（模型 `dumps()` helper）→ 中文无损。

### 9.4 明确不做（本 Step）

```text
无 HTTP 端点（GET /summary 等）      无 SSE summary.*
无 FINALIZING → COMPLETED            无 regenerate / 新版本
无 BackgroundTasks / Celery / Redis / Kafka 队列
无真实 ASR 接入
```

## 10. 明确不做（后续版本）

```text
Agent / Multi-Agent / Memory / RAG / Discussion Node / Todo / Decision
向量数据库 / 跨会议检索 / 声纹身份映射 / 自动议题 / 自动章节
真实麦克风 / 音频上传 / ASR WebSocket / Whisper / FunASR / 音频 Codec / PCM / 采样率
复杂权限 / AI Chat / 知识库 / 思维导图 / AI PPT
```
