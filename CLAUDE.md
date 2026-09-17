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
- **Step 6 已完成**：Transcript Freeze + Finalization Readiness（后端）。
  - freeze = persisted `transcript_stream` ACCEPTED 行（`status=ACCEPTED` +
    `last_segment_seq`）作为 durable freeze boundary；**no schema change**，
    不新增 `frozen` / `finalization_ready` / `FROZEN` 字段，`ACCEPTED` 不改名。
  - 双维度 ingest 门控：`meeting.status` 管生命周期；（freeze 后）
    **PARTIAL after freeze → 409**（进 cache 前）；**new FINAL after freeze → 409**；
    **canonical FINAL retry → 200**。
  - **clear cache only after freeze commit**（`clear_meeting`）；完整性检查失败
    （gap/overrun 409）不得清理。
  - **readiness 从 DB 派生**（service 层）：`stream_service.get_finalization_readiness`
    / `stream_service.is_finalization_ready`，不新增持久化字段 / 公开 API。
  - **meeting remains FINALIZING**；`FINALIZING → COMPLETED` 由 Step 7 Summary
    Pipeline 在 `summary.status` 终态（COMPLETED/FAILED）后驱动，不新增 Meeting FAILED。
- 测试：`backend/scripts/test_finalize_step6.py` 21/21 PASS；
  Step 5 regression 21/21 PASS；parser 2/2 PASS。
- **Step 7A 已完成**：LLMProvider + DeepSeekProvider + Summary Generation Pipeline（后端）。
  - 业务层只依赖 `LLMProvider` 协议（`app/services/llm/base.py`）；`DeepSeekProvider`
    env-only 配置、**未配置 key 是 lazy 失败**（仅首次 `complete()` 抛，不阻断启动）；
    `FakeProvider` 为**默认测试基础设施**（离线确定性）；`LLMProviderFactory` 依据
    `settings.llm_provider` 解析（`fake` 默认 / `deepseek`）。测试注入 `generate(..., provider=...)`。
  - **只有 Freeze 过的 Transcript 能进 LLM**（D6/D13）：meeting 存在 + FINALIZING +
    `is_finalization_ready()`；输入只从**持久化 FINAL**（`list_sorted ASC`），PARTIAL 永不进 prompt。
  - **Phase A/B/C —— 不锁长事务**：A 锁内门控+幂等+upsert GENERATING → B **无锁无事务**
    调 LLM（httpx 同步）+ `MeetingSummarySchema` Pydantic 校验（成功判据）→ C 锁内回写终态。
  - **有界重试 + repair prompt**：`SUMMARY_MAX_ATTEMPTS`（默认 2）；校验失败→带失败原文
    +具体错误的 repair prompt 重试；Provider 级失败（超时/5xx/未配置）不 repair 直接 FAILED。
  - **无静默截断**：`MAX_TRANSCRIPT_CHARS` 硬上界，超限 → FAILED（reason=too large）。
  - **幂等**：初始生成 `version=1`，`UNIQUE(meeting_id, version)`；同 version 已终态
    （COMPLETED/FAILED）重复 generate → **no-op 不覆盖**；并发 double-generate Phase C 同锁裁决。
  - **失败不传导**：仅 `summary.status = FAILED`；**Meeting 保持 FINALIZING**
    （`FINALIZING → COMPLETED` 是 Step 7B，summary 终态后驱动）；崩溃遗留 GENERATING 行可 resume。
  - 明确不做：无 HTTP 端点（GET /summary 等）、无 SSE `summary.*`、无 BackgroundTasks /
    Celery / Redis / Kafka 队列、无真实 ASR。
  - 测试：`backend/scripts/test_summary_step7a.py` 28/28 PASS；
    Step 6 regression 21/21 PASS；Step 5 regression 21/21 PASS；parser 2/2 PASS。
- **Step 7B 已完成**：Finalizer + Summary API + Summary SSE + Meeting Completion（后端）。
  - **触发链**：首个 accepted STREAM_END（`newly_accepted=True`）→ FastAPI
    **BackgroundTasks** 调 `finalizer_service.run_finalizer_background`（**进程内 MVP
    触发，≠ durable job queue**；durable 事实在 DB，可重跑恢复，无启动期全库扫描）。
    相同 STREAM_END retry → `newly_accepted=False`，**绝不重跑 LLM（不重付模型成本）**。
  - **Finalizer**（`finalizer_service.finalize_for_meeting`）：不重实现 Summary
    管线（调用 Step 7A `summary_service`）；幂等（COMPLETED 早退 / 终态 v1 no-op /
    `complete_after_summary` 幂等）；**崩溃缺口可恢复**（显式含"summary 终态 +
    meeting FINALIZING"= 合法可恢复状态，重跑零 LLM）。
  - **Meeting COMPLETED 只在 Summary 终态后驱动**（COMPLETED **或 FAILED** 都驱动；
    绝不新增 Meeting FAILED / FINALIZING_FAILED，D1）。`meeting_service.complete_after_summary`
    是**唯一**完成路径（FINALIZING→COMPLETED / COMPLETED 幂等 / CREATED·IN_PROGRESS 409）；
    路由层永不直接写 `status=COMPLETED`。
  - **Provider 工厂 fail-closed（§1/§24）**：未知 `LLM_PROVIDER` → `LLMConfigurationError`
    （`app/services/llm/base.py`），**绝不回落 FakeProvider**；生成管线当作 provider 级
    失败 → summary FAILED（零 fake 纪要）。
  - **GET /api/meetings/{id}/summary**：最新版本 durable 恢复快照
    （GENERATING/COMPLETED/FAILED 200；meeting 缺失 404 / 无 summary 404）。
  - **POST /api/meetings/{id}/summary/regenerate** → **202** `{version:vN+1, status:
    GENERATING}`；同步分配 GENERATING 行 + `summary.started`，BackgroundTasks 后台跑
    Phase B/C（**自有 session**）；latest GENERATING → 409；版本 **append-only**
    （v1→v2→v3，never overwrite）；**meeting 永不退出 COMPLETED**；输入仍只用持久化
    FINAL ASC（`stream_service.is_frozen` 状态无关门控，D6 在 COMPLETED 仍成立）。
  - **Summary SSE**：`summary.started / completed / failed`（既有 EventBroker）；
    payload 仅 `{version,status}`（**绝不含** raw_model_output/堆栈/API key/prompt）；
    稳定 id `summary:<meeting_id>:<version>:<started|completed|failed>`；
    **非 durable、不回放**（重连用 GET /summary）；commit-before-publish（SSE 失败
    不回滚 DB）；顺序 `summary.started → summary.completed|failed → meeting.status(COMPLETED)`。
  - **canonical FINAL retry 跨 COMPLETED 边界仍 200**（幂等重发 ≠ append；
    新 FINAL / PARTIAL 在 COMPLETED 仍 409）——`transcript_service` 的 `_existing_final`
    短路。
  - 不改 Step 5 恢复协议（Snapshot + `after_segment_seq` 原样；无
    after_summary_version / durable SSE replay / Last-Event-ID）。
  - 明确不做：React/Zustand/Result Page/真实 ASR/Celery/Redis/Kafka/启动期调度。
  - 测试：`backend/scripts/test_summary_step7b.py` 36/36 PASS；
    Step 7A regression 28/28 PASS；Step 6 regression 21/21 PASS（H06 更新为
    "Freeze 后经 Finalizer 终态 COMPLETED"）；Step 5 regression 21/21 PASS；parser 2/2 PASS。


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
