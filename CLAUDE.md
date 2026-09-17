# 小水华会议助手 MVP V0.1

企业内部 AI 会议记录工具。MVP 只跑通两条主链路：

1. 实时会议转写（ASR → Backend → SSE → React 实时字幕）
2. 会后 AI 会议纪要（FINALIZING → Transcript Freeze → LLM → Result）

## 读取顺序（每次改代码前必读）

```text
1. CLAUDE.md               ← 本项目入口
2. prd.md                  ← 业务 / 状态机 / 契约 / 验收 事实源
3. docs/architecture.md    ← 已定架构
4. docs/api-contract.md    ← 已定 API / SSE / Transcript 契约
5. docs/decisions/         ← Accepted ADR
6. fronted_designer.md     ← 前端页面 / 组件 / 视觉
```

## 优先级（冲突时）

```text
PRD
>
Accepted ADR / architecture
>
api-contract
>
frontend design
>
existing implementation
```

除非用户明确要求，否则不得擅自推翻 Accepted 架构决策。

## 技术栈

- 前端：React + TypeScript + Vite + Zustand + Ant Design + Axios + SSE
  （禁止替换为 Vue / Pinia / Element Plus / Next.js / Redux）
- 后端：Python 3.11+ / FastAPI / Pydantic / Uvicorn / httpx / SQLAlchemy
- LLM：公司内部 DeepSeek V4.1 Flash（统一抽象 LLMProvider）
- DB：MVP 用 SQLite（Repository 层 DB 无关，后续可换公司数据平台）

## MVP 范围

只做：Create → Meeting Room → Transcript(PARTIAL/FINAL) → 持久化 → SSE → Finish → FINALIZING → STREAM_END → Freeze → LLM Summary → Result。

不做：Agent / Multi-Agent / Memory / RAG / Discussion Node / Todo / Decision /
向量库 / 真实麦克风音频 / 语音采集 / 声纹身份映射 / 复杂权限 / AI Chat 等。

## 核心不变式

1. Transcript 是会议唯一事实源；Summary 是 AI 派生结果。
2. PARTIAL 只做实时展示，不得写正式 `transcript_segment` 表。
3. FINAL 是唯一可持久化的正式事实（先持久化成功，才能广播 SSE；SSE 广播失败不回滚已提交事务）。
4. Meeting 状态机只有四态：CREATED → IN_PROGRESS → FINALIZING → COMPLETED。
   禁止新增 `FINALIZING_FAILED` / `FAILED`；Summary 失败用 `summary.status = FAILED` 表达。
5. 结束会议必须进入 FINALIZING；必须等待最后 FINAL + `TRANSCRIPT_STREAM_END` +
   完整性确认后才能 Freeze，Freeze 后才允许调 LLM。禁止用固定 `sleep` 等 ASR 尾包。
6. 前端恢复模型固定为 `GET Meeting → GET Transcript Snapshot → 订阅 SSE 增量`。
   SSE 不是事实源；前端断连/刷新/关闭不得影响 Backend 接收与保存。
7. Backend 是 Meeting State + Transcript 的事实源；Frontend 只是 Consumer / Observer。
8. 当前不接真实 ASR：Mock Transcript Producer 模拟 PARTIAL / FINAL / TRANSCRIPT_STREAM_END。
   Backend 只定义并实现 Transcript Event 接收契约。
9. 浏览器不是实际录音源时，禁止做假的麦克风选择器。
10. LLM 必须经过 Provider 层；API Key 不进前端；日志不输出密钥。

完整清单见 `prd.md §61` 与 `docs/architecture.md`。

## Step 进度

- **Step 5 已完成**：Gap-free SSE Incremental Stream + Snapshot → SSE Recovery
  （`GET /api/meetings/{meeting_id}/events`，StreamingResponse）。
- SSE 正式不变式（见 `docs/architecture.md §8.3` 与 `ADR-001 D12`）：
  - Commit-before-publish：FINAL = `Persist → Commit → Publish`；SSE 失败不回滚业务事务。
  - Gap-free handoff：FINAL ingest 与 SSE `connect()` 共用同一 per-meeting lock；
    connect 在锁内"先读 catch-up、后注册订阅" → Snapshot/Catch-up/Live handoff 不丢、
    不因 connect race 双投。**非端到端 exactly-once**：Consumer 用
    `final:<meeting_id>:<segment_seq>` 去重。
  - PARTIAL 只 live 推送、不重放；`TRANSCRIPT_STREAM_END` 不广播（PRD §30 未列出）。
  - EventBroker 进程内（非 durable log）；慢消费者队列满 → 驱逐，绝不阻塞 Producer。
  - 心跳 `settings.sse_heartbeat_interval`；`after_segment_seq` 是唯一恢复游标
    （`Last-Event-ID` 未实现）；`after_segment_seq < 0` → 400。
- 测试：`backend/scripts/test_sse_step5.py` 21/21 PASS + 2 parser 单测。

## 常用命令

```bash
# 后端
cd backend
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000

# 依赖
cd backend && .venv/Scripts/python -m pip install -r requirements.txt
```

## 文档索引

- `prd.md` — 产品 / 技术契约 / 状态机 / 验收
- `fronted_designer.md` — 前端设计
- `docs/architecture.md` — 架构与原则
- `docs/api-contract.md` — API / SSE / Transcript 契约（与实际实现一致）
- `docs/decisions/ADR-001-mvp-architecture.md` — MVP 架构决策
