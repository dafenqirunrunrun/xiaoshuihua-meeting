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

- 同一 utterance 的后续 PARTIAL 覆盖旧值，禁止无限 append。
- 收到对应 utterance 的 FINAL 后，删除该 cache 条目。

### 5.2 FINAL

固定处理顺序（禁止先广播后落库）：

```text
Receive
 ↓ Validate
 ↓ Idempotency Check
 ↓ Persist
 ↓ Commit Success
 ↓（后续 Step：SSE Broadcast）
```

- FINAL 必须持久化成功后再广播（SSE 为后续 Step，本节先留扩展点）。
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

## 9. 明确不做（后续版本）

```text
Agent / Multi-Agent / Memory / RAG / Discussion Node / Todo / Decision
向量数据库 / 跨会议检索 / 声纹身份映射 / 自动议题 / 自动章节
真实麦克风 / 音频上传 / ASR WebSocket / Whisper / FunASR / 音频 Codec / PCM / 采样率
复杂权限 / AI Chat / 知识库 / 思维导图 / AI PPT
```
