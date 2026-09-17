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
