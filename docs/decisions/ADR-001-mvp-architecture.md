# ADR-001: MVP 架构基线

- 状态：**Accepted**
- 日期：2026-09-17
- 作者：小水华会议助手 MVP 项目组
- 关联：`prd.md`、`docs/architecture.md`、`docs/api-contract.md`

## 背景

MVP V0.1 需要固化一组跨团队、跨 Agent 的架构决策，避免后续开发（尤其多 Agent /
前后端并行）各自引入实现差异。本文档编列已被批准的基线决策，作为业务代码实现的硬约束。

## 决策

### D1. Meeting 状态机固定四态

```text
CREATED → IN_PROGRESS → FINALIZING → COMPLETED
```

禁止新增 `FINALIZING_FAILED` / `FAILED` 等 Meeting 状态。
**动机**：PRD §11 状态机、§12 明确"AI 失败 ≠ 会议失败"。失败必须通过 Summary 状态表达：
`meeting.status = COMPLETED` + `summary.status = FAILED`。

### D2. ASR 为外部上游 Producer

- 当前不接真实 ASR；语音系统负责音频/ASR 全链路。
- Meeting Backend 只定义并实现 **Transcript Event 接收契约**，不做音频相关处理。
- 开发阶段用 **Mock Transcript Producer** 模拟 PARTIAL / FINAL / TRANSCRIPT_STREAM_END。
- 真实 ASR 未来按契约接入，不改变业务系统。

### D3. Backend 是事实源，Frontend 是 Consumer

- Backend = Meeting State + Transcript Source of Truth。
- Frontend 断网 / 刷新 / 关闭 / SSE 断开均不得影响 Backend 接收与持久化。
- 前端恢复模型固定为 `GET Meeting → GET Transcript Snapshot → SSE 增量`；SSE 非事实源。

### D4. PARTIAL / FINAL 严格分离

- PARTIAL：进程内 cache（key = meeting_id + utterance_id），不落正式表，可覆盖。
  - 生命周期为 **Backend Process Scoped Temporary State**，与 Frontend / Browser Session /
    SSE Connection 完全解耦。没有 SSE 连接时 Backend 依然持续接收并维护 PARTIAL。
  - 进程重启后 PARTIAL 丢失可接受（非 Source of Truth）。
- FINAL：唯一允许持久化的正式事实。
- 处理顺序固定：Receive → Validate → Idempotency → Persist → Commit Success → (SSE 后续)。
  禁止先广播后落库。

### D5. FINAL 幂等与冲突语义

- 完全相同的 FINAL 重发 → 幂等成功，不重复 INSERT（`UNIQUE(event_id)`）。
- 同 event_id 内容不同 → 409；同 (meeting_id, segment_seq) 内容相同（不同 event_id）→ 幂等；
  内容不同 → 409。禁止 silent overwrite。

### D6. 不依赖固定 sleep 等待 ASR 结束

- 用户 Finish 进入 FINALIZING ≠ Transcript 完整。
- 必须等待 `TRANSCRIPT_STREAM_END`（含 `last_segment_seq`）+ 完整性确认 + Freeze 后才能调 LLM。
- 禁止 `sleep(3)` 等猜测式等待。

### D7. 分层与领域异常

- 分层：api/routes（HTTP）→ services（业务）→ repositories（SQLAlchemy）。
- Router 不做业务逻辑；Service 不 import FastAPI/HTTPException。
- 领域异常集中映射：NotFound→404、Validation→400、State/Conflict→409。

### D8. 并发控制范围（明确限制）

- 实现：进程内 `threading.RLock` + SQLite busy timeout。
- 适用场景：single-process MVP，仅保证进程内确定性。
- **不构成**生产级跨进程锁。未来多进程/多实例/PostgreSQL 必须升级为
  `SELECT ... FOR UPDATE`、conditional UPDATE 或乐观锁（version column）。
- 不为未来场景过度设计当前实现。

### D9. MVP 不做清单

Agent / Multi-Agent / Memory / RAG / Discussion Node / Todo / Decision / 向量库 /
跨会议检索 / 声纹映射 / 自动议题 / 真实音频链路 / 复杂权限 / AI Chat / 知识库。

### D10. PARTIAL cache 生命周期 = Backend Process Scoped

- `live_utterance_cache` 生命周期与 Frontend / Browser Session / SSE Connection 完全解耦。
- 没有任何 SSE 连接时 Backend 依然持续接收 PARTIAL、更新 cache、接收 FINAL、持久化 FINAL。
- 进程重启 PARTIAL 丢失可接受（非 Source of Truth）。多实例 → Redis。

### D11. STREAM_END 必须持久化 + 共享锁（Step 4）

- STREAM_END 落盘 `transcript_stream`（重启后幂等 retry 仍 200，Case 12 实证）。
- FINAL ingest 与 STREAM_END 完整性检查共享同一 per-meeting RLock
  （`lock_registry.meeting_lock`），互斥于同一个同步边界。
- 一个 Stream 只有一个确定终点：接受 `last=3` 后 `last=4` → 409。
- 缺 `last_segment_seq` 必须不是 append-only 起点：新 FINAL after end → 409；
  已存在 FINAL 幂等 retry → 200。
- 当前为 single-process 方案；multi-worker / multi-instance / PostgreSQL 必须升级为
  数据库级并发控制（见 D8）。不得把进程内锁当作跨进程锁。

### D12. SSE Broker + gap-free handoff + slow-consumer 策略（Step 5）

**Broker 定位**：`EventBroker` 是**进程内**的增量通知通道（Broker ≠ durable event log）。
`meeting_id → set[Subscriber]`，每个 Subscriber 一个有界 `asyncio.Queue`。
重启丢订阅者 / 队列——恢复靠 **DB Snapshot + 持久化 FINAL catch-up**，不靠重放 broker。
multi-instance / Redis / Kafka 为后续演进，不在本 Step 实现。

**Commit-before-publish**：FINAL ingest 在 meeting 锁内 `persist → commit → publish`；
SSE 广播失败（无订阅者 / 队列满 / 断开）**绝不回滚**已提交业务事务；Producer 线程
只做 bounded `put_nowait`（经 `call_soon_threadsafe` 调度到订阅者事件循环），永不阻塞。

**Gap-free handoff（connect 同锁原子）**：`GET /events?after_segment_seq=N` 的
`connect()` 在 per-meeting 锁内：先读持久化 `seq > N`（catch-up）、后注册订阅者。
FINAL ingest 与 connect 用同一把锁 → catch-up 查询与订阅注册之间不存在 FINAL 提交窗口，
服务端 **Snapshot → Catch-up → Live handoff 不丢 FINAL、不因 connect race 双投**。
注意：这**不是端到端 exactly-once**；网络断连 / 客户端游标推进不及时仍可能重放，
Consumer 应以稳定 SSE id `final:<meeting_id>:<segment_seq>` 或 `segment_seq` 去重。

**Slow-consumer 策略**：订阅者队列满（`sse_queue_maxsize`）→ 驱逐该订阅者
（发 disconnect sentinel 使其退出，移除订阅），**绝不让 Producer / FINAL commit 阻塞**。

**PARTIAL 不重放**：只 live 推送；断线不补（恢复靠 Snapshot + catch-up）。
`TRANSCRIPT_STREAM_END` 不广播（PRD §30 未列出，契约不可扩展）。
### D13. Transcript Freeze = durable 边界 + 派生 readiness（Step 6）

- Freeze 边界复用 `transcript_stream` ACCEPTED 行（`status=ACCEPTED` + `last_segment_seq`），
  不新增 `frozen` / `finalization_ready` / `FROZEN` 字段或状态，不改名 `ACCEPTED`。
- 双维度 ingest 门控：`meeting.status` 管 Meeting 生命周期，
  `transcript_stream ACCEPTED` 管 Transcript 是否冻结。
- Freeze 后 PARTIAL → 409，且不得修改 cache。
- Freeze 后新 FINAL → 409；canonical existing FINAL retry → 200。
- cache 只在 freeze commit 成功后 `clear_meeting`；完整性检查失败不得清理。
- Finalization Readiness 为派生状态，已实现：
  `stream_service.get_finalization_readiness`
  `stream_service.is_finalization_ready`
  从 DB 推导，不新增持久化字段或公开 API。
- Step 6 完成后 Meeting 仍为 FINALIZING。
  Step 7 Summary Pipeline 中，当 `summary.status` 进入终态 `COMPLETED` 或 `FAILED` 后，
  再将 `meeting.status` 置为 `COMPLETED`。
  Summary 失败不新增 Meeting FAILED / FINALIZING_FAILED。

### D14. LLM Provider 抽象 + Summary 生成管线（Step 7A）

- **业务层不绑定具体 LLM**：`summary_service` 只依赖 `LLMProvider` 协议
  （`app/services/llm/base.py`：`name` + `complete(prompt) -> str`）。
  实现：`DeepSeekProvider`（env-only 配置，OpenAI 兼容 chat/completions，httpx 同步）与
  `FakeProvider`（**默认测试基础设施**，离线确定性）。工厂 `LLMProviderFactory`
  依据 `settings.llm_provider`（`fake` 默认 / `deepseek`）解析。
  **Step 7B 修订（§1/§24，见 D15）**：任何其它值 → `LLMConfigurationError`
  （fail-closed），**绝不回落 FakeProvider**——生产误配必须大声失败，
  不得静默生成假纪要。测试通过 `generate(..., provider=...)` 注入。
- **未配置 Key 是 lazy 失败**：`DeepSeekProvider` 构造不触网，仅首次 `complete()`
  抛 `SummaryGenerationError`（→ summary FAILED），绝不阻断 import / startup。
- **只有冻结过的 Transcript 才能进 LLM**（D6/D13）：Start 门控 = meeting 存在 +
  `status == FINALIZING` + `is_finalization_ready()`（ACCEPTED stream-end）。
  输入只从**持久化 FINAL**（`list_sorted ASC`）读取，PARTIAL 永不进 prompt。
- **不锁长事务**：生成分 Phase A（锁内短事务：门控 + 幂等 + upsert GENERATING）→
  Phase B（**无锁、无事务**：读 FINAL ASC → build prompt → provider.complete →
  `MeetingSummarySchema` Pydantic 校验，可 repair 重试）→ Phase C（锁内回写终态）。
  LLM 调用期间不持任何 DB 锁 / 事务。单进程 MVP 用同一 per-meeting lock（D8）。
- **有界重试**：结构化校验失败 → **repair prompt**（携带失败原文 + 具体校验错误，
  重新喂同一 `<transcript>` 素材）→ 重试，至多 `summary_max_attempts`（默认 2）。
  `summary_parse_repair=False` → 首次失败即 FAILED。Provider 级失败（超时/5xx/未配置）
  不做 repair，直接 FAILED。
- **无静默截断**：`MAX_TRANSCRIPT_CHARS` 硬上界；transcript 超限 → FAILED
  （reason=transcript too large），绝不静默丢弃内容。
- **幂等**：初始生成固定 `version=1`，`UNIQUE(meeting_id, version)`；
  同 version 已终态（COMPLETED / FAILED）的重复 generate → **幂等 no-op，不覆盖**
  （显式 regenerate + 新版本留给 Step 7B）。并发 double-generate 由 Phase C 同锁裁决：
  后到者若发现已写终态 → 409，**不覆盖赢家**。
- **失败不传导到 Meeting 状态机**：`summary.status = FAILED` + reason；Step 7A 中
  Meeting **保持 FINALIZING**，任何结果都**不驱动 COMPLETED**
  （`FINALIZING → COMPLETED` 是 Step 7B，当 summary 进入终态后触发，见 D13）。
- **崩溃恢复**：Phase A 对遗留 `GENERATING` 行支持 resume（Phase C 回写终态），
  不会留下孤儿 GENERATING 或重复终态行。

### D15. Finalizer + Summary 生命周期 + Meeting Completion（Step 7B）

链路：`首个 accepted STREAM_END → 初始 Summary 生成 → Summary 终态
（COMPLETED / FAILED）→ Meeting FINALIZING → COMPLETED → summary.completed|failed
SSE → meeting.status SSE`。

- **触发 = 首个 accepted STREAM_END**（`newly_accepted=True`）：BackgroundTasks
  调 `run_finalizer_background`（**进程内 MVP 触发，≠ durable job queue**；durable
  事实都在 DB，可由重跑 Finalizer 恢复，**不做启动期全库扫描**）。
  相同 STREAM_END retry → `newly_accepted=False`，**绝不重跑 LLM / 重付模型成本**。
- **Finalizer 幂等**：meeting COMPLETED → 早退；summary v1 已终态 → `generate()`
  no-op（**零 LLM 调用**）；`complete_after_summary` 幂等。
- **崩溃缺口可恢复**：`summary 终态 + meeting FINALIZING` 是合法可恢复状态
  （Step 7A Phase C 先提交 summary）；Finalizer 重跑只补 Meeting 状态。
- **Summary 终态驱动 Meeting COMPLETED**：COMPLETED **和 FAILED 都**驱动
  FINALIZING → COMPLETED；**绝不新增 Meeting FAILED / FINALIZING_FAILED**（D1）。
  Meeting COMPLETED 只在 Summary 终态**之后**（绝不 GENERATING 期间 / STREAM_END 后立即）。
- **`complete_after_summary` 是唯一完成路径**（meeting_service）：
  FINALIZING → COMPLETED 合法，COMPLETED → COMPLETED 幂等，CREATED / IN_PROGRESS
  非法（409）。路由层永不直接写 `meeting.status = "COMPLETED"`。
- **GET /summary = durable 恢复快照**（最新版本，GENERATING/COMPLETED/FAILED 均 200；
  meeting 缺失 404 / 无 summary 404）。前端重连模型 = GET Meeting + GET Summary +
  Transcript Snapshot + SSE。
- **Summary SSE 非 durable、不回放**：事件只在内存 EventBroker（D12）；
  重连取 GET /summary。payload 仅 `{version, status}`——**绝不含** raw_model_output /
  provider 堆栈 / API key / prompt。commit-before-publish（SSE 失败不回滚 DB）。
  顺序不变式：`summary.started → summary.completed|failed → meeting.status(COMPLETED)`。
- **regenerate append-only 版本**：`POST /summary/regenerate` → 202
  `{version: vN+1, status: GENERATING}`，同步分配 GENERATING 行 + `summary.started`，
  BackgroundTasks 后台跑 Phase B/C（自己的 session）。latest GENERATING → 409；
  版本永不覆盖（v1→v2→v3）。**regenerate 永不把 Meeting 移出 COMPLETED**（§19）。
  regenerate 输入仍只用持久化 FINAL ASC（`stream_service.is_frozen` 状态无关门控，
  D6 在 COMPLETED 会议同样成立）。
- **BackgroundTasks session 生命周期**：后台任务**自有** `SessionLocal()`，
  绝不复用请求 session（请求 session 在响应后关闭）；异常仅记录，
  durable 事实可经重跑 Finalizer / regenerate 恢复。
- **provider 门控 fail-closed**：`LLMProviderFactory` 对未知 `LLM_PROVIDER` 抛
  `LLMConfigurationError`（D14 修订）；生成管线把它当 provider 级失败 → summary
  FAILED（**绝不静默生成 fake 纪要**）。
- **canonical FINAL retry 跨 COMPLETED 边界仍 200**（幂等重发 ≠ append；
  新 FINAL / PARTIAL 在 COMPLETED 仍 409）——与 STREAM_END retry 对 COMPLETED
  放行一致（见 `transcript_service.handle_event` 的 `_existing_final` 短路）。

## 后果

- **正面**：跨团队契约可先于真实 ASR 稳定下来；前后端可并行开发；事实清晰可验收。
- **负面 / 需要记录**：
  - RLock 在 multi-worker 下无效（D8 已注明升级路径）。
  - FINAL "完整相同"比较基于 (text, speaker_id, start_ms, end_ms)，若上游时序字段枚举方式变化需同步修订契约。
  - `emitted_at` 当前类型为 epoch 毫秒整数（PRD §15）；若真实 ASR 发送 ISO 字符串需在契约层显式修订（ADR 记录在案）。
- **拒绝的替代方案**：
  - 用 Meeting 失败态表达 Summary 失败（违背 D1，误导"会议失败"）。
  - 先 SSE 后落库（违背 D4，SSE 丢失即事实丢失）。
  - 固定 sleep 等 ASR 尾包（违背 D6）。
