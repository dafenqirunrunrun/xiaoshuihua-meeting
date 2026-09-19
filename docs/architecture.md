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

### 6.1 Frontend SSE 消费实现（Step 8B-4 已实现）

> 消费 Step 5 的 gap-free 契约；**Backend 零业务修改**（前端纯 Consumer）。

```text
Snapshot FIRST → SSE SECOND
  GET Meeting
    ↓
  GET Transcript Snapshot          ← 持久化事实（durable source of truth）
    ↓
  maxSegmentSeq = 最后一个 FINAL 的 segment_seq（无 FINAL → 0，backend 允许）
    ↓
  GET /api/meetings/{id}/events?after_segment_seq=maxSeq
```

- **连接顺序不可倒置**：Snapshot 是恢复基线，SSE 只是实时通知通道。
- **Stream 生命周期**：CREATED / COMPLETED 不连（或关闭）；IN_PROGRESS / FINALIZING
  建立并保持。`Finish → FINALIZING` **不关流**：ASR 可能仍发 trailing FINAL
  （Finish ≠ Transcript Stream End）。unmount / meetingId 变化必须 close EventSource。
- **PARTIAL ephemeral / FINAL durable**：SSE 只负责 **live projection**；PARTIAL 是
  临时态（keyed by `utterance_id` 原地覆盖，FINAL 落定即删，不持久化、不写
  localStorage）；FINAL 以 `segment_seq` 前端去重 + 保持 ASC，Snapshot catch-up /
  重连重复不会造成重复展示。
- **meeting.status 事件**：前端不猜 timestamps / 不构造 Meeting —— 收到后调用
  GET Meeting，用 server 响应覆盖 store（Server 仍是 Meeting Source of Truth）。
  这样 FINALIZING → COMPLETED 由后端真正状态驱动（Summary 事件交给 8C）。
- **断线恢复（onerror）**：浏览器默认自动重连不可靠（恢复协议是
  Snapshot + after_segment_seq），so：主动 close → `clearPartialUtterances`（临时态）
  → 指数退避（1s/2s/4s/8s/max 10s，成功连接后重置）→ 重新 GET Snapshot →
  setFinalSegments → 重算 maxSeq → 以新游标重建 SSE。**绝不依赖 Last-Event-ID**。
- **单连接不变量**：React StrictMode 的 cleanup + `gen` 计数 + `openStream` 单飞
  保证任意时刻每个 meeting 最多一个 active EventSource；旧会议的 reconnect timer
  在 meetingId 变化时清除（gen 失效），不污染新会议。
- **auto-scroll 不影响数据语义**：底部附近自动跟随（新 PARTIAL/FINAL 或 PARTIAL
  文本变长）；用户向上查历史则暂停跟随，显示「有新内容 · 回到底部」，点击恢复。
  纯 UI 行为。

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
  finalization_ready=true，也**绝不在 Step 6 变 COMPLETED**（COMPLETED 由 Step 7B
  Finalizer 在 Summary 终态后驱动）。
- **Step 7B 补充**：`stream_service.is_frozen(db, meeting_id)` 是**状态无关的
  仅 freeze 检查**（ACCEPTED 行 + `last_segment_seq`），供 regeneration 在
  COMPLETED 会议上仍然满足 D6"仅 frozen transcript 进 LLM"（§11.3）。

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
    支持 `"fake"`（默认）与 `"deepseek"`；**任何其它值 → `LLMConfigurationError`
    （fail-closed）**——绝不回落 FakeProvider（Step 7B §1/§24，见 ADR D15）。
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
无 BackgroundTasks / Celery / Redis / Kafka 队列（Step 7A 约束）
无真实 ASR 接入
```

> Step 7B 变更：GET /summary、POST /summary/regenerate、`summary.*` SSE、
> FINALIZING → COMPLETED、以及 **BackgroundTasks 作为进程内 MVP 触发（非 durable
> job queue）** 均在 Step 7B 实现 —— 见 §11 与 ADR D15。

## 10. 明确不做（后续版本）

```text
Agent / Multi-Agent / Memory / RAG / Discussion Node / Todo / Decision
向量数据库 / 跨会议检索 / 声纹身份映射 / 自动议题 / 自动章节
真实麦克风 / 音频上传 / ASR WebSocket / Whisper / FunASR / 音频 Codec / PCM / 采样率
复杂权限 / AI Chat / 知识库 / 思维导图 / AI PPT
```

## 11. Finalizer + Summary API + Summary SSE + Meeting Completion（Step 7B 已实现）

> 契约：`首个 accepted STREAM_END → 自动触发初始 Summary 生成 → Summary 终态
> （COMPLETED / FAILED）→ 才允许 Meeting FINALIZING → COMPLETED`。补充 GET /summary
> 恢复快照、POST /summary/regenerate（append-only 版本）、`summary.*` SSE。
> 本 Step 不做 React / Zustand / 前端（Step 8）。ADR：**D15**。

### 11.1 Finalizer（`app/services/finalizer_service.py`）

- **职责边界**：协调 Summary 终态 → Meeting COMPLETED；**不重新实现 Summary 管线**
  （prompt / retry / schema 校验 / DeepSeek / 持久化都在 Step 7A `summary_service`）。
- **触发**：首次 accepted STREAM_END（`newly_accepted=True`）→ FastAPI BackgroundTasks
  调 `run_finalizer_background(meeting_id)` → 自己的 DB session（`SessionLocal`，
  绝不复用请求 session，§23）。
- **幂等**（任何重入安全）：
  - meeting 已是 COMPLETED → 早退（跳过 LLM + 状态迁移）；
  - summary v1 已终态 → `generate()` 幂等 no-op，**零 LLM 调用**；
  - `complete_after_summary()` 幂等（FINALIZING → COMPLETED）。
- **崩溃缺口可恢复**：`summary 终态 + meeting FINALIZING` 是合法可恢复状态
  （Step 7A Phase C 先提交 summary）；Finalizer 重跑只补 Meeting 状态，不重调 LLM。
- **绝不创建 Meeting FAILED / FINALIZING_FAILED**（ADR D1）；Summary 失败用
  `summary.status = FAILED` 表达，且同样驱动 Meeting COMPLETED。
- **FakeProvider 注入**：`finalize_for_meeting(..., provider=...)` 透传（测试）。

### 11.2 Meeting Completion（`meeting_service.complete_after_summary`）

- **唯一** Meeting 完成路径：FINALIZING → COMPLETED（`completed_at` 首次置位）。
- FINALIZING 但 Summary 未终态 → 该路径根本不会被调用（Finalizer 是唯一调用方，
  且它在 Summary 终态后才调）；CREATED / IN_PROGRESS → 409（非法）。

### 11.3 Summary API（HTTP）

- **GET /api/meetings/{id}/summary**（§15）：最新版本 Summary 快照 —— durable
  恢复事实源（GENERATING / COMPLETED / FAILED 均 200）。前端重连模型：
  GET Meeting + GET Summary + Transcript Snapshot + SSE。meeting 不存在 →
  404 "meeting not found"；存在但无 summary → 404 "summary not found"。
- **POST /api/meetings/{id}/summary/regenerate**（§17-§22）→ **202**
  `{meeting_id, version, status: "GENERATING"}`（不阻塞 LLM）：
  1. 同步分配 vN+1 + 持久化 GENERATING 行 + 发 `summary.started`（`create_regeneration`）；
  2. BackgroundTasks → `run_regenerate_background`（自己的 session，`complete_generation`）；
  3. 返回 202。
- 约束：meeting 必须是 FINALIZING **或 COMPLETED**（regeneration 永不把 Meeting
  移出 COMPLETED，§19）；latest 已是 GENERATING → **409**（§18，避免重复模型成本）；
  版本只 append（v1→v2→v3，never overwrite）；输入仍只来自**持久化 FINAL ASC**
  （`is_frozen` 门控；不因 COMPLETED 而失效，§21）。
- 无 `force=True` 混入；终态不覆盖。

### 11.4 Summary SSE

- 通过既有 EventBroker（`GET /events`），`event_type` 扩展为 `summary.started /
  summary.completed / summary.failed`。
- **Commit-before-publish**：DB commit → SSE publish；SSE 失败（驱逐/异常）
  **绝不回滚**业务事务（Finalizer 对终态 publish 做了 best-effort try/except）。
- **payload 最小化**：`{version, status}`；**绝不**含 raw_model_output / provider
  堆栈 / API key / prompt。
- **稳定 id**：`summary:<meeting_id>:<version>:<started|completed|failed>`。
- **非 durable**：Summary 事件**不回放**（`after_segment_seq` 只回放 FINAL）；
  重连客户端用 GET /summary 恢复（§15/§28）。
- **顺序不变式**（§12/B21）：`summary.started → summary.completed|failed →
  meeting.status(COMPLETED)`（终态事件在终端 Summary durable 之后）。

### 11.5 明确不做

```text
不接 / 不替换 Step 5 恢复协议（Snapshot + after_segment_seq 原样保留，
  无 after_summary_version / durable SSE replay / Last-Event-ID）
React / Zustand / Result Page / 真实 ASR
Celery / Redis / Kafka / 启动期全库扫描调度
BackgroundTasks 仅作进程内 MVP 触发（≠ durable job queue；durable 事实在 DB）
```

## 12. React Frontend Foundation（Step 8A 已实现）

> 前端工程基础。技术栈：React 18 + TypeScript + Vite + React Router 6 + Zustand +
> Axios + Ant Design 5（fronted_designer §4/§45/§62 Step 1–6）。目录：`frontend/`。

### 12.1 结构

```text
frontend/src/
  app/        App.tsx（ConfigProvider theme + AntdApp + RouterProvider）、router.tsx
  layouts/    AppShell.tsx（侧栏：首页 · 会议 · 设置）
  pages/      Home / Meetings / MeetingRoom / MeetingResult / Settings / NotFound
  components/ MeetingStatusTag、CreateMeetingModal（8B/8C 扩展）
  stores/     meetingStore.ts（shape + reset，8B 填 reducer）
  api/        http.ts（Axios + ApiError 归一化）、meeting.ts、transcript.ts、summary.ts
  types/      meeting.ts、transcript.ts、summary.ts、sse.ts（严格映射后端 schema）
  styles/     designTokens.ts（ConfigProvider theme 唯一入口）、global.css
```

### 12.2 对齐契约的原则

- **Types 不猜**：`MeetingStatus` 严格四态（CREATED / IN_PROGRESS / FINALIZING /
  COMPLETED，ADR D1）；`TranscriptSegment/TranscriptSnapshot`（TranscriptSegmentOut）、
  `MeetingSummary`（SummaryOut 含 version）、`SseEnvelope`（SSEEventEnvelope，含
  payload 按 event_type 区分）—— 全部源自 `backend/app/schemas/*`。
- **API client**：只封装实际存在的后端端点；业务组件不直接 `axios`（fronted_designer
  §44/§61.10）。错误经 `ApiError` 分 http/network/unknown。
- **前端恢复模型不变式（PRD §32，arch §6）不变**：GET Meeting → GET Transcript
  Snapshot → GET Summary（Step 7B）→ SSE 增量。SSE 非事实源。
- **Meeting 状态驱动页面**：四态分别 = CREATED「待开始」/ IN_PROGRESS「会议进行中」/
  FINALIZING「处理中」/ COMPLETED「已完成」；**无 FINALIZING_FAILED**（fe 不显示
  "会议失败"；纪要失败由 `meeting=COMPLETED + summary=FAILED` 表达，PRD §12）。
- **PARTIAL / FINAL 硬分离**：`partialUtterances`（keyed by utterance_id，原地覆盖）
  与 `finalSegments`（按 segment_seq，append/upsert）为 store 中两个独立字段
  （PRD §34/§35）。
- **Mock**：Step 8 不做前端 Mock Stream 主路径（VITE_USE_MOCK_STREAM 仅保留为
  fronted_designer §58 说明）；8B/8C 直接连真实后端（真实 HTTP/SSE + Mock Transcript
  Producer + Fake LLMProvider）。
- **Vite proxy**：`/api → http://127.0.0.1:8000`（post-dev CORS 无关，浏览器同源）；
  `VITE_API_BASE_URL` 为部署覆盖项（.env.example 已提交，真实 .env 不提交）。

### 12.3 明确不做（8A）

```text
无 SSE / Snapshot 恢复（8B）          无 Summary / Regenerate / Result 渲染（8C）
无前端 Mock Stream 协议              不改后端 / 不新增 GET /api/meetings 列表
无 API Key 输入框（Browser 不收 DeepSeek Secret，PRD §45/arch §10）
```

### 12.4 Meeting Workspace UI Polish（Step 8B-5 已实现）

> 纯展示层打磨：不改变任何数据语义 / SSE 协议 / store 结构 / 四态机。
> 视觉体系：冰蓝工作台（Primary `#3478F6` / AI-Live Mint `#20B486` /
> Page BG `#F6F9FC` / Card `#FFF` / Border `#E8EEF5`），内容 max 1200px。
> 8B-1~8B-4 的 JSX 若需位移，只移动 JSX，绝不重实现数据逻辑。

- **Design Tokens**（`designTokens.ts` 唯一数据源）：新增 `colorMint/colorMintLight`、
  `shadowCard/shadowCardHover`、`motionFast/motionBase`、`railWidth`；radius 升为
  10/12/16，`contentMaxWidth 1200`；`antdTheme` 同步 Menu / Button / Card / Tabs 圆角。
- **Home Hero**：左文右画 ——「小水华，让每一次会议更清晰」+ 唯一 Primary CTA
  「创建会议」+ 次级「查看会议」；右侧 `HeroIllustration`（inline SVG：浮起转写卡片 /
  波形 live 线 / 呼吸圆点 / 植物，纯装饰 `pointer-events:none`，窄屏淡出）。
- **最近会议 / Meetings / Settings**：真实空态（无 fake 列表）；Settings 改为
  分组入口（模型设置 / 数据管理 / 关于我们），明确「后续版本开放」，无 API Key 输入。
- **Meeting Room 四态展示**（数据逻辑原样）：
  - CREATED：圆形麦克风 + `ripple-ring` 呼吸环 +「准备开始会议」+ 真实「开始会议」；
  - IN_PROGRESS：section head `● 实时记录` + hint「AI 实时转写 · 自动保存」；
  - FINALIZING：section head「正在整理会议」+ 三态指示条（✓会议已结束 / ●转写收尾中 /
    ○AI纪要等待生成，无 fake 百分比）；
  - COMPLETED：section head「会议记录」+ 完成横幅「会议已完成」+「查看会议结果」
    CTA（`navigate(/meetings/:id/result)`）。
  - Transcript FINAL = meeting-notes（speaker 弱化小字、次行），PARTIAL = 次级轻量 +
    小薄荷 live 点 +「识别中」，自动滚动 FAB 悬浮右下。
- **微交互（CSS-only）**：按钮 hover 轻抬 2px / active scale(0.98)、卡片 hover
  上升 + 阴影增强、侧栏选中浅蓝胶囊、FAB 出现轻升起 + 点击微压、live-dot 2.2s 呼吸。
  `@media (prefers-reduced-motion: reduce)` 关闭全部 transform / 脉冲 / 平滑滚动。
- **响应式**：`≥1200` 双列 / `900–1199` 窄轨（260px）/ `<900` 单列 + 右轨下移。
- **明确不做**：Framer Motion / GSAP 等动画库；fake 数据（无会议列表 / 参会人头像 /
  时间戳 / 摘要）；无 API Key 输入；Meeting 四态与 Summary 契约不变；不进入 8C。


## 13. Meeting Discovery & Resume（Step 8B-6 已实现）

> 解决产品断点：用户离开 Meeting Room 后，若不知道 `/meetings/:id` 就找不到进行中
> 的会议。新增最小列表查询 + 首页/列表页发现，让任何状态的会议都能被重新找到并进入。

### 13.1 后端：GET /api/meetings

- 最小列表接口（service `list_meetings`）：`status?` 过滤 + `limit?/offset?` 分页；
  默认 `limit=20`，最大 `100`；非法 `status` / 越界 `limit` / 负 `offset` → **422**。
- 排序 `updated_at DESC, created_at DESC`（最近更新的在前）；`total` 为过滤后总数
  （不计分页）。响应 `MeetingListOut{items,total,limit,offset}`，item 复用 `MeetingOut`
  （**不新增派生字段**，不把逻辑塞进 schema）。
- 路由顺序：`GET /api/meetings` 先于 `GET /api/meetings/{meeting_id}` 注册。
- 无 user isolation：MVP 单租户仓库，Meet 全量可见（PRD 未引入用户体系）。

### 13.2 Resume 语义：Resume = 导航，不是生命周期

- **`Resume` 是 `GET /meetings/:id` 的页面导航**，不是新 endpoint：
  `POST /resume` 不存在，Meeting **从不 Pause / Resume 状态跳转**。
  进入页面 = 既有恢复模型（arch §6）：GET Meeting → GET Transcript Snapshot →
  `maxSegmentSeq` → SSE `after_segment_seq` 增量。IN_PROGRESS / FINALIZING /
  COMPLETED / CREATED 四态都能被重新进入。
- **离开页面 ≠ 生命周期迁移**：用户点「返回会议列表」（纯导航 `navigate('/meetings')`，
  页面 header 左上角）离开 Meeting Room，**不结束 / 不暂停会议**；会议继续 IN_PROGRESS，
  后端照常接收并保存转写。绝不出现「退出会议 / 暂停录音」之类文案（**Pause 不是已实现
  功能**，PRD §60 明确不做；无 PAUSED / RECORDING_PAUSED / MeetingStatus.PAUSED）。
- 页面刷新同理：refresh = re-enter = 重跑恢复模型，Backend 状态不受影响（RES-15）。

### 13.3 前端发现（Home / Meetings）

- **Home**：真实 Meeting List 分区（无 fake）。`limit=50` 一次拉取：
  - 正在进行（Active）：`IN_PROGRESS` / `FINALIZING`，最多 3 张卡片 +「查看全部会议」
    （进入 `/meetings`）。这张卡片 = 离开 Meeting Room 后重新发现并 Resume 的入口。
  - 最近的会议（Recent）：`CREATED`（继续准备）+ `COMPLETED`（查看结果）。
  - loading / error / 真实空态均轻量；保留唯一 Primary CTA「创建会议」。
- **Meetings**：真实列表页替代占位。状态 Tabs（全部 / 待开始 / 进行中 / 收尾中 /
  已完成）→ 真实后端 `status=` 过滤；每页 20（与 `LIST_DEFAULT_LIMIT` 一致）。
- **列表项 CTA（状态驱动，唯一语义）**：
  - `CREATED → 继续准备`（`/meetings/:id` 会前准备页）
  - `IN_PROGRESS → 继续会议`（`/meetings/:id`，Resume 主路径）
  - `FINALIZING → 查看收尾`（`/meetings/:id`）
  - `COMPLETED → 查看结果`（`/meetings/:id/result`，8C 前为骨架）
  - 绝不出现「重新开始 / 继续录音」——这些语义不存在。
- 时间文本全部来自真实后端字段（创建于 / 开始于 / 结束于 / 完成于 + 真实时长），
  无 fake 时间戳。

### 13.4 明确不做（8B-6）

```text
无 POST /resume / 无 Pause / 无 PAUSED / RECORDING_PAUSED / MeetingStatus.PAUSED
无 user isolation 假逻辑（单租户全量可见）     无翻页 UI（limit=20 单页，可后续加）
无 AI Chat / 无真实 ASR / 无 Redis / Kafka   不进入 8C（Result / Summary 渲染）
```

## 14. Result Page Recovery（Step 8C-1 已实现）

> Result 页 = 归档阅读态：展示**持久化**的 Meeting + Summary + FINAL Transcript。
> 刷新后从 Backend 全量恢复，不依赖 Zustand 临时状态（PRD §54）。
> 与 MeetingRoom（实时态）职责分离：Result **不连接 Transcript SSE**，不消费
> summary.* SSE（实时更新留 8C-2）。

### 14.1 恢复模型

```
/meetings/:id/result
├── GET Meeting（useMeetingLoad，复用 MeetingRoom 模式）
├── GET Summary Snapshot（latest version，Step 7B §15，POST /summary 返回）
└── GET Transcript Snapshot（仅 FINAL，segment_seq ASC，PRD §32/§48）
```

三个数据域**独立加载、独立错误、独立 retry**：
- Meeting 域失败（404「会议不存在」/ network「会议加载失败」+ 重试）→ 页面级轻量态。
- Summary 域失败（404 `summary not found` = 会议存在但纪要未生成；网络失败 →
  「纪要加载失败」+ 重新加载）**不影响** Meeting / Transcript。
- Transcript 域失败 → 「会议内容加载失败」+ 重新加载，**不影响** Summary / Meeting。

### 14.2 Summary 状态展示（actual enum：NOT_STARTED / GENERATING / COMPLETED / FAILED）

- `COMPLETED` → 真实分节（会议概述 / 核心议题 / 主要结论 / 未决问题，来自 SummaryOut
  `overview / key_topics / conclusions / open_questions`；空数组弱化，不显示大空卡片）。
- `GENERATING` / `NOT_STARTED` / meeting=FINALIZING 且 summary 未生成 → 「AI 纪要正在
  生成」+ 轻量「刷新状态」（手动重新 GET，不做自动轮询）。
- `FAILED` → 「AI 纪要生成失败」；**Meeting 仍显示 COMPLETED**（Summary FAILED ≠
  Meeting FAILED，PRD §12）；Transcript Tab 仍可查看。
- version：append-only；8C-1 只展示 latest（弱位置显示「版本 vN」，无版本切换）。

### 14.3 404 语义区分（actual backend contract）

- Meeting 不存在 → GET /summary 与 GET /meeting 均 404 `meeting not found: ...`；
  前端 Meeting 域先捕到「会议不存在」。
- 会议存在但 Summary 未生成 → GET /summary 404 `summary not found`；前端归为
  「纪要尚未生成」/「AI 纪要正在生成」（按 Meeting 状态），绝不误显示「会议不存在」。

### 14.4 明确不做（8C-1）

```text
无 Regenerate / 重新生成按钮（8C-3）             无版本切换 / 对比
无自动轮询（手动「刷新状态」兜底，SSE 通知见 §15）  无 Meeting 删除 / 编辑
```

## 15. Summary Realtime Notification（Step 8C-2 已实现）

> Result 页在 8C-1 基础上新增 **Summary 生命周期实时通知**：
> SSE 只是**通知通道**，GET /summary 才是 **durable 事实源**。
> 「生成中 → 完成/失败」自动过渡，无需手动刷新；刷新/断线仍按 Snapshot 恢复。

### 15.1 角色与连接条件

```
/meetings/:id/result（summary 非 terminal 时）
├── GET Meeting
├── GET Summary Snapshot（初始 latest）
├── GET Transcript Snapshot
└── EventSource /api/meetings/:id/events?after_segment_seq=0（仅 Summary 生命周期）
```

连接条件（useSummaryResultStream）：
- **仅当 summary 未达 terminal** 才建立 SSE —— meeting 仍 FINALIZING（纪要生成中 /
  即将生成），或 meeting COMPLETED 且 summary GENERATING（重新生成中）。
- meeting COMPLETED 且 summary 终态（COMPLETED/FAILED）→ **不连**（无需通知，
  手动刷新兜底；GET /summary 即快照）。
- summary terminal 后 **关闭 EventSource**；重新生成开始则下次 render 自动重建。

### 15.2 SSE 事件处理（只通知，不取数）

| 事件 | 前端动作 |
| --- | --- |
| `summary.started` | 转「正在生成」（不造假进度/百分比） |
| `summary.completed` | **重新 GET /summary** → 渲染持久化快照 |
| `summary.failed` | **重新 GET /summary** → 渲染 FAILED 状态 |
| `meeting.status(COMPLETED)` | **重新 GET /meeting**（Server 是状态事实源） |

- **绝不**把 SSE `{version, status}` payload 直接当 Summary 渲染 —— 那是通知，
  数据一律来自 GET /summary（SSE 非 durable、不回放，重连靠 GET）。
- 消费到的任何 summary 事件都只触发一次轻量 GET；GET 失败不影响已有展示
  （Summary 域独立 error/retry 保留）。

### 15.3 断线恢复（Snapshot-first）

- onerror → 主动 close → 指数退避（1s/2s/4s/8s/max10s）→ **GET /summary**：
  - 若已 terminal → 不重建 SSE（已覆盖最终态）；
  - 若仍 GENERATING/missing → 重建 SSE（新 EventSource）。
- 与 MeetingRoom 的 useMeetingStream Trascript-Snapshot-recovery 同构，只是恢复源
  换成 GET /summary。任何时刻最多一个 EventSource（StrictMode gen 计数防护）。

### 15.4 硬约束

- Result 是归档阅读态：**不消费 transcript.partial / final**（Transcript 由
  useTranscriptSnapshotResult 从 GET 恢复）。
- 不调用 regenerateSummary（8C-3 才加按钮）；不产生假进度；不暴露 SSE 技术细节给用户。
- meetingId scope —— 每个会议独立 EventSource，切换/离开即关闭。

### 15.5 明确不做（8C-2）

```text
无 Regenerate / 重新生成按钮（8C-3）   无版本切换 / 对比（8C-3）
无 Transcript 实时流（Result 归档态）  无 Pause / ASR 控制
```

## 16. Summary Regeneration（Step 8C-3 已实现）

> Result 页在 8C-1/8C-2 基础上新增 **主动重新生成纪要**：把 Step 7B 已存在的
> `POST /summary/regenerate` 接进前端。本 Step **不重新开发 Agent**，只做接线。

### 16.1 不变式：Meeting 全程 COMPLETED

```text
regenerate 前后 Meeting 状态恒为 COMPLETED。
绝不 COMPLETED -> FINALIZING（不重跑 Transcript Freeze、不重跑 Finalizer）。
Transcript 不受影响：输入仍是已冻结的持久化 FINAL（ASC），
    不重传 Transcript、不重连 Transcript live SSE、不改 FINAL 段。
```

### 16.2 版本 append-only（唯一分配者是 Backend）

```text
v1 COMPLETED --regenerate--> v2 GENERATING --> v2 COMPLETED | v2 FAILED
旧版本永不覆盖 / 永不删除（UNIQUE(meeting_id, version)）。
前端**绝不**自行 version++，**绝不**猜下一个版本号 —— 一律以 Backend 返回/GET 为准。
```

### 16.3 前端接线（最小扩展，复用 8C-2）

- **入口**：会议纪要区标题行右侧「重新生成纪要」（secondary button + Popconfirm
  「重新生成会创建一个新的纪要版本，当前纪要不会被删除。」/ 取消 / 重新生成；非 danger）。
- **两种 loading 严格区分**：`submitting` = POST 在飞行中（按钮「正在提交…」）；
  `generating` = Backend latest = GENERATING（按钮禁用 + Banner）。
- **触发**：调用既有 `regenerateSummary(meetingId)`（页面禁止直接 axios/fetch）。
- **不清空旧纪要**：保留最近一次 COMPLETED 快照（`lastGood`），GENERATING / FAILED
  期间继续展示，配轻量 Banner（「正在生成新版纪要 / 当前纪要仍可查看，生成完成后将自动更新。」）。
- **SSE 重建是关键**：v1 terminal 时 8C-2 已关闭 SSE；POST 成功 → 立刻 GET /summary
  → 发现 latest = v2 GENERATING → `terminal` 转 false → `useSummaryResultStream` 重建 SSE。
- **失败不传导**：HTTP 网络错误只表示"请求失败"，**绝不**把 Summary 置为 FAILED
  （FAILED 只能来自 Backend 持久化状态）；409 → 「纪要正在生成中，请稍候。」+ GET /summary 同步。
- 只展示 latest，无版本历史 / 切换 / 对比 / 删除。

### 16.4 后端缺陷修复（Step 7B 遗漏，最小修复）

`run_regenerate_background` 原先只发 `summary.started`，**从不发**
`summary.completed|failed` —— 重新生成完成后 Result 页永远收不到终态通知
（页面卡在「生成中」）。已按其 Finalizer 同构方式补齐：终态行 durable 提交后
发布 `summary.completed|failed`（commit-before-publish；发布失败不回滚，SSE 不是事实源）。
顺序契约不变：`summary.started → summary.completed|failed`。

### 16.5 前端缺陷修复：具名 SSE 帧（关键，影响 8B + 8C-2/8C-3）

后端 `_render_frame` 每帧写 `event: {event_type}`（具名帧，契约见
`docs/api-contract.md` L194-195）。按 SSE 规范，**具名帧不触发 `onmessage`**，
只有未具名帧才触发。而两个前端 hook 都只 `addEventListener('message')`：

```text
useMeetingStream.ts       -> 8B 实时字幕（PARTIAL / FINAL）被静默丢弃
useSummaryResultStream.ts -> 8C-2/8C-3 summary.* 通知收不到（页面永不自动刷新）
```

修复：新增 `frontend/src/utils/sse.ts` 的 `subscribeSseEnvelopes(es, cb)`，
按 `SSE_EVENT_TYPES` 逐事件名订阅（并兼容 `message`），两个 hook 改用它。
**后端零改动**（协议按契约本就是具名帧）。

### 16.6 明确不做（8C-3）

```text
无版本历史 / 版本切换 / 版本对比 / 删除版本
无 Agent 重开发（复用既有 Summary pipeline）
无 Time Travel / 无手动编辑纪要 / 无导出
```

---

## 17. ASR Transcript Input Adapter（Step ASR-0A 已实现）

> 目标：让 Backend 直接消费协作者约定的**标准 ASR 输入格式**，而不改动既有
> Transcript Domain。边界：**ASR provider-specific 格式止步于 Adapter**。
> 本 Step 不接真实 Tencent ASR、不动 DB schema、不改 LLM Provider、不改状态机。

### 17.1 ASR Boundary

```text
Audio / Tencent ASR                     ← 协作者负责
      ↓
Normalized PARTIAL / FINAL              ← 协作者契约（camelCase，无 segmentSeq）
      ↓
[External DTO]  asr_schemas.py
      ↓
[Adapter]       asr_adapter.py          ← 纯翻译：字段映射 + identity 派生
      ↓
[Ingress]       asr_service.py          ← 唯一的 segment_seq 分配点（锁内）
      ↓
Transcript Domain（既有，未改动）
  live_cache / repository / commit / publish_final
      ↓
SSE（具名帧） → React 实时字幕 → Snapshot / Resume → Freeze → Summary Agent
```

两个 ingress 并存，两个契约互不污染：

| 路由 | 契约 | 命名 | `segment_seq` |
| --- | --- | --- | --- |
| `/api/internal/meetings/{id}/transcript-events` | **内部**（PRD §15） | snake_case | **Producer-owned**（不变） |
| `/api/internal/meetings/{id}/asr-events` | **外部协作者** | camelCase | **Transcript-owned**（本 Step 新增） |

既有内部路由与 Step 5/6 测试**完全未改动**。

### 17.2 External DTO / Internal DTO 分离

外部 DTO 是 camelCase，内部 Domain 保持 snake_case；**没有**为了接协作者格式
把整个 Python Domain 改成 camelCase。

```text
External (camelCase)      Internal (snake_case)
  type                      status
  meetingId                 meeting_id
  asrSessionId              → 仅用于派生 identity，不落库
  sentenceId                → 仅用于派生 identity，不落库
  speakerId (int, -1)       speaker_id (str, "unknown" / "speaker_N")
  startMs / endMs           start_ms / end_ms
  provider                  → 不落库
```

外部 DTO 采用 `extra="forbid"`：多一个字段即契约漂移，必须 422 大声失败，
而不是静默忽略。只接受 camelCase 一种写法（snake_case 会被拒绝）。

### 17.3 segment_seq 归属（本 Step 核心决策）

**协作者不提供 `segmentSeq`**（ASR 的 `sentenceId` 是 session 内编号，与会议级
Transcript 全局顺序不是同一概念）。**禁止 `segment_seq = sentenceId`**：ASR
重连会开新 session、句号从头发，会与 `UNIQUE(meeting_id, segment_seq)` 冲突
并破坏全局顺序。

分配规则（`asr_service._resolve_final_segment_seq`）：

```text
next_seq = (max persisted segment_seq) + 1
```

**读取 max → 分配 → 持久化 必须在同一 meeting lock 临界区内**，否则并发 FINAL
会取到相同 seq。实现上 `ingest_asr_event` 全程持有 `meeting_lock(meeting_id)`，
`handle_event` 再进入同一把锁（per-meeting `RLock`，同线程可重入）→ 临界区连续。

**幂等重试不消耗新 seq**：分配前先按派生 `event_id` 查已持久化行；命中则**复用**
其 `segment_seq`，让重试落进既有 Case A 幂等路径（200、不 INSERT）。否则重试会
分配新尾号、错过 Case A、撞 `UNIQUE(meeting_id, segment_seq)`。

### 17.4 utterance identity 与 event identity 分开

```text
utterance_id = f"{asrSessionId}:{sentenceId}"        ← PARTIAL 原地覆盖 & PARTIAL→FINAL 交接
FINAL event_id = "asrf_" + sha256(meetingId|asrSessionId|sentenceId|FINAL)[:32]
PARTIAL event_id = "asrp_" + sha256(meetingId|asrSessionId|sentenceId|PARTIAL|text|startMs|endMs)[:32]
```

- `utterance_id` 对同一句的全部 PARTIAL 与对应 FINAL **恒等** → 前端只显示一条
  会原地变化的字幕，绝不堆成多条。
- PARTIAL **不**复用该句的固定 event_id：一句会发多个 revision。其摘要混入
  revision 内容（text + timing），保证不同 revision id 不同、相同重试 id 相同。
  （`handle_partial` 从不读 `event_id`、也不落库，因此这里只要求确定性与可追溯性。）
- FINAL 摘要含 `meeting_id`，因为 `UNIQUE(event_id)` 是**全局**而非 meeting 内。
- `asrSessionId` 上限 48 字符：`utterance_id` 必须落进内部 64 字符界（最坏 48+1+10=59）。
  超长在边界直接 422，而不是静默截断或散列 identity。

### 17.5 FINAL idempotency 冲突不会被静默吞掉

`event_id` 是**确定性**派生的，所以"同一 ASR sentence identity、内容却不同"会命中
Case A。既有 `handle_final` 的处理是**先 canonical 比对、不同即 409**（不是无条件
200）：

```text
transcript_segment 已有该 event_id
  ├─ text/speaker/start_ms/end_ms 完全相同 → 200（幂等重试，不重复 INSERT）
  └─ 任一不同                            → 409 TranscriptConflictError
```

即：内容漂移会显式报冲突，不会被当成重试吞掉。

### 17.6 speakerId = -1

`-1` 表示"说话人暂时未知"，是**合法值**（早段 PARTIAL 本来就不知道说话人），
永不报错。映射到内部 `unknown` 哨兵，**绝不**产生 `speaker_-1` 到达持久化或前端。

### 17.7 provider metadata 的限制

`provider` / `asrSessionId` / `sentenceId` 只存在于 External Ingress / Adapter 的
生命周期内，用于 identity 派生，**不落库**。

**LIMITATION**：DB 无对应列，本 Step 明确不新增列（不做 schema migration）。因此
这些 metadata 重启后不可查；如需按 session/provider 追溯，需后续 Step 的新增列 +
索引（届时与 SQLite → 公司数据平台迁移一并评估）。

内部**不硬编码腾讯**：`provider` 是不透明字段，`tencent` / `whisper` / `aliyun`
走同一条通路（ASR-A12 已覆盖）。

### 17.8 并发范围（MVP 单进程）

sequence 分配依赖 `meeting_lock` —— 一个**进程内** per-meeting `RLock` 注册表。
它在单进程内使"读 max → 分配"原子，这正是 MVP 的全部部署形态；它**不是**跨进程 /
跨实例保证。

**FUTURE**：multi-worker / MySQL 阶段必须把分配升级为**数据库级安全机制**
（`SELECT ... FOR UPDATE` / 条件更新 / 乐观版本号），与 ADR D8 同一议题。
本 Step 不实现。

### 17.9 Mock Producer

`scripts/mock_transcript_producer.py` 已改写为**模拟协作者输出**（不再模拟我们的
内部 DTO）：camelCase、带 `asrSessionId` / `sentenceId`、`speakerId=-1`、多段
PARTIAL revision、**不发送 `segmentSeq`**。它仍是**纯外部 HTTP 客户端**，不 import
任何 Backend 内部模块。

它发送的 `STREAM_END` 是**当前内部 mock 终结信号**，见 §17.10。

### 17.10 PENDING COLLABORATOR CONFIRMATION

> 以下两项**尚未**与协作者锁死，本 Step 不单方面宣布为双方最终协议。
> `protocols/transcript-format.md` 属共同协议，本 Step **未修改**其核心字段。

**PENDING 1 — `TRANSCRIPT_STREAM_END` 契约**

```text
未确认：谁发送 / 何时发送 / payload 字段 / last_segment_seq 归属
现状：  继续使用项目内既有 internal mock STREAM_END，仅用于完整链路回归
        （freeze → summary → COMPLETED）。它不是双方最终协议。
```

**PENDING 2 — multi-ASR-session 时间轴语义**

```text
未确认：同一 meeting 多个 ASR session（重连）时，startMs/endMs 是否重置
现状：  只保证单 session（mock-session-001）正确；不猜测多 session 行为
```

### 17.11 本 Step 明确不做

```text
真实 Tencent ASR / ESP32 / PCM / 音频上传 / 腾讯 WebSocket / 腾讯签名
真实 LLM 配置 / DeepSeek 接线（继续 FakeProvider）
SQLite → MySQL / DB schema change / 新 UNIQUE / migration
Harness 登录鉴权 / Cloud
Recording Pause / Resume（有 asrSessionId ≠ 要实现录制生命周期）
Discussion Node / Todo / Decision
STREAM_END 双方协议重设计 / 多 session 时间轴规则
```

### 17.12 验证

- `backend/scripts/test_asr_adapter.py` —— ASR-A01~A20，**20/20 PASS**
- `backend/scripts/e2e_asr_browser.py` —— 真实 Chrome (CDP) **14/14 PASS**
  （含 named SSE 真实到达、PARTIAL 原地覆盖、FINAL 替换、刷新 Snapshot 恢复）
- 回归全绿：Step5 21 + Step6 21 + Step7A 28 + Step7B 36 + list 17
- 前端 `npm run typecheck` + `npm run build` PASS
- **Backend DB schema 零改动；LLM Provider 零改动；Meeting 状态机零改动**
