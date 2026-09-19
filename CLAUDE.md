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
- **Step 8A 已完成**：React Frontend Foundation（前端基础设施）。
  - 技术栈落地（固定，禁换）：React 18 + TypeScript + Vite 5 + React Router 6
    + Zustand 4 + Axios + Ant Design 5 + @ant-design/icons。
  - 目录结构（对齐 `fronted_designer.md §45`）：`src/app`（App + router）/
    `layouts`（AppShell）/ `pages`（Home · Meetings · MeetingRoom · MeetingResult ·
    Settings · NotFound）/ `components` / `hooks`（8B 用）/ `stores`（meetingStore）/
    `api`（http · meeting · transcript · summary）/ `types` / `utils` / `styles`。
  - **API Client 只对后端实际契约封装**（未猜）：`createMeeting` / `getMeeting` /
    `startMeeting` / `finishMeeting` / `getTranscriptSnapshot` / `getSummary` /
    `regenerateSummary`；`http.ts` 统一 Axios + 极薄错误归一化
    （http status / backend detail / network error，`ApiError`）。
  - **Types 严格映射后端 schema**：`meeting`（四态 CREATED / IN_PROGRESS /
    FINALIZING / COMPLETED，**无 FINALIZING_FAILED**）/ `transcript`
    （TranscriptSegment / TranscriptSnapshot / PartialUtterance）/ `summary`
    （MeetingSummary · SummaryStatus / RegenerationAccepted）/ `sse`
    （SseEventType / SseEnvelope / StreamStatus）。
  - **Zustand store（8A 仅 shape + reset，redducer 8B 填）**：`meeting` /
    `finalSegments`（FINAL）/ `partialUtterances`（keyed by utterance_id，
    PARTIAL）/**两者硬分离** / `summary` / `streamStatus` / `resetMeetingState`。
  - **路由**（fronted_designer §7）：`/` / `/meetings` / `/meetings/:meetingId` /
    `/meetings/:meetingId/result` / `/settings`；`AppShell` 侧栏 = 首页 · 会议 · 设置。
  - **Design Token**：`src/styles/designTokens.ts` → Ant Design `ConfigProvider theme`
    统一入口（色值来自 fronted_designer §47，零散 magic number 只在一处）。
  - **Vite proxy**：`/api → http://127.0.0.1:8000`（业务代码不硬编码后端 host）；
    `VITE_API_BASE_URL` 仅作部署覆盖；`.env.example` 提交、真实 `.env` 禁止提交。
  - Home 页真创建（POST /meetings + /start → navigate(`/meetings/${id}`)）；
    Meetings 页为 **MVP 占位**（后端无 GET /meetings 列表，不改后端加接口）。
  - Home / Meetings / MeetingResult / MeetingRoom / Settings 均为 Step 8A 骨架
    （SSE / Snapshot 恢复 / Summary 渲染在 8B/8C）。**不做前端 Mock Stream 主路径
    （VITE_USE_MOCK_STREAM）** —— 8B/8C 直连真实后端（真实 HTTP/SSE + Mock
    Producer + Fake LLMProvider）。
  - 验证：`npm install` + `npm run typecheck` + `npm run build`（vite build PASS）；
    `npm run dev` 起 5173，`/ /meetings /settings /meetings/test /meetings/test/result`
    路由可达（SPA fallback 200），Vite proxy `/api/health → 8000` 连通。
  - **Backend 零修改**（Step 8A 未动 `backend/`）。
  - 前端命令见下方「常用命令」。
- **Step 8B-1 已完成**：Meeting Snapshot + CREATED pre-meeting 态（`7bf6af4`）。
- **Step 8B-2 已完成**：Start / Finish Lifecycle（`31a6ba9`）。
  - 创建 ≠ 开始：CREATED 展示「开始会议」→ POST /start → IN_PROGRESS；
    IN_PROGRESS 展示「结束会议」（Popconfirm）→ POST /finish → FINALIZING。
  - Server 是 Source of Truth：start/finish 均以 backend 返回的 Meeting 覆盖 store，
    前端绝不自行写 status / started_at / COMPLETED；错误展示后端 409 detail。
  - FINALIZING / COMPLETED 无 start/finish 按钮；finish ≠ completed（绝不预置 COMPLETED）。
- **Step 8B-3 已完成**：Transcript Snapshot（`ae17b38`）。
  - GET Meeting 成功 → GET /transcript Snapshot → `setFinalSegments`（FINAL 仅 ASC）。
  - Snapshot 是已持久化 FINAL 的历史事实源；CREATED 不拉；IN_PROGRESS/FINALIZING/
    COMPLETED 均可看历史。失败只影响 Transcript 区（轻量重试），不动 meeting。
- **Step 8B-4 已完成**：Realtime Transcript SSE + Auto Scroll（文档见 `architecture.md §6.1`）。
  - **恢复模型**：GET Meeting → GET Transcript Snapshot → `maxSegmentSeq` →
    `GET /events?after_segment_seq=maxSeq`（Snapshot FIRST，SSE 只是通知通道）。
  - `useMeetingStream`：EventSource 生命周期 + 断线 Snapshot-recovery 指数退避
    （1s/2s/4s/8s/max10s）+ `meeting.status → GET Meeting` 覆盖 store（不猜状态）。
  - `useMeetingStream.ts` store：`upsertPartial`（utterance_id 原地覆盖）/
    `removePartialUtterance`（FINAL 落定删除）/ `clearPartialUtterances`（恢复前清空）/
    `setStreamStatus`；FINAL 去重以 `segment_seq` 为准 + 保持 ASC。
  - CREATED / COMPLETED 不连流；Finish → FINALIZING **不关流**（trailing FINAL 仍收）；
    unmount / meetingId 变化 close + 清游标；StrictMode 单 EventSource（gen 计数）。
  - `useAutoScroll`：底部附近跟随 / 向上查历史暂停 + 「回到底部」/ 首次 instant。
    纯 UI，不影响数据语义。PARTIAL 次级轻量（opacity），FINAL 正式稳定。
  - **Backend 零业务修改**；未处理 summary.* 事件（8C）。
- **Step 8B-5 已完成**：Meeting Workspace UI Polish（`style(frontend): polish meeting workspace experience`，未 push）。
  - **纯展示层**：不改变任何数据语义 / SSE 协议 / store 结构 / 四态机（8B-1~8B-4 逻辑原样保留）。
    视觉体系 = 冰蓝工作台：Primary `#3478F6` / AI-Live Mint `#20B486` / Page BG `#F6F9FC` /
    Card `#FFF` / Border `#E8EEF5` / Text `#172033·#48556A·#8A97AA`；radius 10/12/16；
    内容 max 1200px；边框优先、阴影极弱（hover 略升）。
  - **Design Tokens**（`designTokens.ts` 唯一数据源）：新增 `colorMint/colorMintLight`、
    `shadowCard/shadowCardHover`、`motionFast/motionBase`、`railWidth`；`antdTheme`
    Menu/Button/Card/Tabs 圆角对齐。CSS 变量见 `global.css :root`。
  - **Home Hero**：左文右画 ——「小水华，让每一次会议更清晰」+ 唯一 Primary CTA「创建会议」
    + 次级「查看会议」；`HeroIllustration.tsx`（inline SVG：浮起转写卡片 / 波形 live 线 /
    呼吸圆点 / 植物，`pointer-events:none`，窄屏淡出）。最近会议保持真实空态。
  - **Meeting Room 四态展示**（数据逻辑零改动）：CREATED = 圆形麦克风 + `ripple-ring`
    呼吸环 +「开始会议」（真实 handleStart）；IN_PROGRESS = `● 实时记录` + hint
    「AI 实时转写 · 自动保存」；FINALIZING =「正在整理会议」+ 三态指示条
    （✓会议已结束 / ●转写收尾中 / ○AI纪要等待生成，无 fake 百分比）；COMPLETED =
    「会议已完成」横幅 +「查看会议结果」CTA（`navigate(/meetings/:id/result)`）。
  - **Transcript**：FINAL = meeting-notes（speaker 弱化小字 + 正文次行，非聊天气泡）；
    PARTIAL = 次级轻量 + 小薄荷 live 点 +「识别中」+ 原地覆盖；FAB 悬浮右下。
  - **微交互（CSS-only）**：按钮 hover -2px / active 0.98、卡片 hover 上升阴影增强、
    侧栏选中浅蓝胶囊 + hover 图标轻推、FAB 出现轻升 / 点击微压、live-dot 2.2s 呼吸；
    `@media (prefers-reduced-motion: reduce)` 全停 transform / 脉冲 / 平滑滚动。
    无 Framer Motion / GSAP。
  - **Responsive**：`≥1200` 双列 / `900–1199` 窄轨 260px / `<900` 单列 + 右轨下移；
    hero 插画窄屏淡出；无横向滚动。
  - **侧栏**：品牌 + 导航（选中浅蓝胶囊）+ 底部 tagline「让每一次会议，都有回响。」
    （浅薄荷装饰板）。
  - **Meetings / Settings / Result**：头部 + 副标题文案；Meetings 真实空态；Settings
    分组入口（模型设置 / 数据管理 / 关于我们），无 API Key 输入；Result 双 Tab + 四区
    纪要骨架原样。
  - **验证**：`npm run typecheck` PASS + `npm run build` PASS；7 张截图（Home /
    Meetings / Room-CREATED / IN_PROGRESS / FINALIZING / Result / Settings）通过
    headless Chrome 生成；UI-01~UI-14 回归（创建 / 开始 / PARTIAL / FINAL / 结束 /
    FINALIZING / COMPLETED / 结果页跳转）实测通过；**Backend 零修改**。
  - 明确不做：fake 数据（无会议列表 / 头像 / 时间戳 / 摘要）、动画库、API Key 输入；
    不进入 8C。
- **Step 8B-6 已完成**：Meeting Discovery & Resume（`e51a97d` + `7870978` + `c70ea10`）。
  - 后端 `GET /api/meetings`：`status?/limit?/offset?` 过滤 + 分页（默认 20 / 最大 100），
    复用 `MeetingOut`；排序 `updated_at DESC, created_at DESC`；路由先于 `GET /{id}`；
    非法 status/limit/offset → 422。测试 `backend/scripts/test_list_meetings.py` 17/17。
  - **Resume = 导航不是生命周期**：`/meetings/:id` 进入 = GET Meeting → Snapshot → SSE；
    Meeting 从不 Pause（无 PAUSED / POST /resume）；离开页面 ≠ 结束/暂停（header
    「返回会议列表」纯导航）。
  - Home 发现（Active IN_PROGRESS/FINALIZING ≤3 + Recent CREATED/COMPLETED）与
    Meetings 真实列表（状态 Tabs → 后端 status 过滤；状态 CTA：继续准备 / 继续会议 /
    查看收尾 / 查看结果）。无 user isolation。
  - 修复：`useMeetingList` 依赖稳定标量（`e0e3372`）—— 修复首页无限请求循环抖动；
    Result 页加「返回」按钮（navigate(-1) / fallback /meetings）。
  - 验证：后端回归全绿（list 17 + step5 21 + step6 21 + step7A 28 + step7B 36）；
    typecheck + build PASS；浏览器四态 resume + 各 Tab 过滤实测。
- **Step 8C-1 已完成**：Summary Snapshot & Real Result Page。
  - Result 页 = 归档阅读态，恢复模型 `GET Meeting → GET Summary Snapshot（latest） →
    GET Transcript Snapshot（仅 FINAL ASC）`，刷新后从 Backend 全量恢复，不依赖
    Zustand 残留；**不连 Transcript SSE、不消费 summary.\* SSE**（实时更新留 8C-2）。
  - 三个数据域独立 load/error/retry：Meeting（useMeetingLoad 复用）/ Summary
    （`useMeetingSummary`）/ Transcript（`useTranscriptSnapshotResult`）。任一失败
    不拖垮其它域。
  - Summary 状态展示（actual enum NOT_STARTED/GENERATING/COMPLETED/FAILED）：
    COMPLETED 分节（概述/核心议题/结论/未决）；GENERATING/NOT_STARTED/FINALIZING
    → 「AI 纪要正在生成」+ 手动「刷新状态」；FAILED → 「生成失败」，Meeting 仍
    COMPLETED，Transcript 仍可看；404 区分 meeting 不存在 vs summary 未生成。
  - 只展示 latest version（弱位置 vN），无版本切换 / 无 Regenerate（8C-3）。
  - **Backend 零修改**；前端仅 Result 页 + 2 个新 hook + global.css 补 classes。
  - 验证：完整 Happy Path 实测（meeting_092bbe38，3 FINAL → COMPLETED → summary v1
    COMPLETED），浏览器覆盖 C1-01~C1-15（含 F5 refresh 恢复、Meetings→查看结果、
    FINALIZING not-ready、FAILED、404、网络错误独立 retry）。
- **Step 8C-2 已完成**：Summary Realtime SSE（Result 页实时通知，`feat(frontend): stream summary generation status`）。
  - **SSE 只当通知通道，GET /summary 是 durable 事实源**：summary.started / completed /
    failed / meeting.status 到达 → 只触发 GET（/summary 或 /meeting），**绝不**把 SSE
    `{version,status}` payload 直接当 Summary 渲染（summary events 非 durable、不回放）。
  - 新 hook `useSummaryResultStream`（Result 专用，**不复用/不碰 useMeetingStream**）：
    仅 summary 非 terminal（meeting FINALIZING，或 COMPLETED + summary GENERATING）
    才连 SSE；terminal 后关闭；断线恢复 = 指数退避（1s/2s/4s/8s/max10s）+ **GET /summary**
    → 已 terminal 则不重连，否则重建；StrictMode gen 计数保证单 EventSource。
  - Result 页 `useSummaryResultStream` + `useMeetingSummary`（重 GET）协作：摘要生成中
    显示「AI 纪要正在生成」（不造假进度）→ summary.completed 到达 → 自动 GET → 渲染
    真实持久化摘要；手动「刷新状态」保留为 fallback（降权）。
  - **不消费 transcript.partial/final**（Result 归档态）；不调用 regenerateSummary
    （8C-3 加按钮）；不暴露 SSE 技术细节给用户。
  - **Backend 零修改**；前端仅 `useSummaryResultStream.ts`（新建）+ MeetingResultPage
    （接 hook）。
  - 验证：后端回归 7A 28/28 + 7B 36/36 PASS；typecheck + build PASS；浏览器 E2E
    （新建 FINALIZING → Result 页停留 SSE 已连 + 「AI 纪要正在生成」→ 后台 stream-end
    → **无刷新**自动转真实摘要 COMPLETED + terminal 后 SSE 关闭）；COMPLETED 页不连
    SSE + 刷新恢复摘要；FAILED 页显示生成失败且不连 SSE。
- **Step 8C-3 已完成 ✅**：Regenerate Summary（主动重新生成纪要）。
  - 入口：Result 页会议纪要区标题行右侧「重新生成纪要」（secondary button +
    Popconfirm「重新生成会创建一个新的纪要版本，当前纪要不会被删除。」/ 取消 / 重新生成）。
  - **Meeting 全程 COMPLETED**：绝不 `COMPLETED → FINALIZING`，不改 lifecyle；
    Transcript 不受影响（仍用已冻结的持久化 FINAL，不重传 / 不重连 live SSE）。
  - **版本 append-only，唯一分配者是 Backend**：v1 → v2 → v3…，旧版永不覆盖；
    前端**绝不** `version++`、不猜版本号。
  - 两种 loading 严格分离：`submitting`（POST 在飞，「正在提交…」）vs
    `generating`（latest=GENERATING，按钮禁用 + Banner）；GENERATING 期间**不清空**
    旧纪要（`lastGood` 保留 + 「正在生成新版纪要」Banner）。
  - **SSE 重建（§9 关键）**：v1 terminal 时 8C-2 已关流 → POST 后立刻 GET /summary
    → 发现 latest=GENERATING → `terminal` 转 false → `useSummaryResultStream` 自动重连。
  - 失败不传导：网络错误只表示"请求失败"（绝不置 Summary=FAILED）；409 →
    「纪要正在生成中，请稍候。」+ GET /summary 同步；Meeting 仍 COMPLETED。
  - 修复（后端最小修复，Step 7B 遗漏）：`run_regenerate_background` 原先**从不发**
    `summary.completed|failed` → 重新生成完成后页面永远收不到终态。已按 Finalizer
    同构补齐（终态 durable 后发布，commit-before-publish）。
  - **修复（前端关键缺陷，影响 8B + 8C-2/8C-3）**：后端 `_render_frame` 发**具名帧**
    （`event: <event_type>`），按 SSE 规范具名帧**不触发 `onmessage`**，而两个 hook
    都只监听 `message` → 8B 实时字幕与 8C-2 summary.* 通知**全部静默失效**。
    新增 `frontend/src/utils/sse.ts` `subscribeSseEnvelopes()` 逐事件名订阅，
    `useMeetingStream` + `useSummaryResultStream` 改用它。**Backend 零改动**。
  - 验证：8C-3 E2E 24/25（唯一"失败"为冷刷新下无旧内容可显示，符合 §19）；
    边界 E2E 32/32 PASS（FAILED / 409 / 网络错误 / 导航离开回来 / Transcript 回归）；
    8B 实时字幕回归 6/6 PASS；后端回归 7A 28/28 + 7B 36/36 + Step6 21/21 +
    Step5 21/21 + list 17/17；typecheck + build PASS。
- **Step 8C Summary Frontend Chain 完成 ✅**（8C-1 Snapshot + 8C-2 Realtime +
  8C-3 Regenerate）。
- **Step ASR-0A 已完成**：Transcript Input Adapter & Mock Contract Integration。
  - **ASR Boundary**：`Audio/Tencent ASR → Normalized PARTIAL/FINAL → External DTO
    → Adapter → Ingress → 既有 Transcript Domain → SSE → React`。provider-specific
    格式**止步于 Adapter**；内部 Domain 保持 snake_case，未因接协作者格式改成 camelCase。
  - **两个 ingress 并存**：既有 `POST /api/internal/.../transcript-events`（内部契约、
    snake_case、`segment_seq` **仍 Producer-owned**）**零改动**；新增
    `POST /api/internal/meetings/{id}/asr-events`（外部协作者、camelCase、
    `extra="forbid"`、**无 segmentSeq**）。
  - **segment_seq = Transcript-owned（仅新路由）**：FINAL 由 `asr_service` 在
    `meeting_lock` 内分配 `max + 1`；**幂等重试复用已存 seq**（否则会撞
    `UNIQUE(meeting_id, segment_seq)`）。**禁止 `segment_seq = sentenceId`**
    （sentenceId 是 session 内编号，重连会重号）。
  - **identity 分开**：`utterance_id = f"{asrSessionId}:{sentenceId}"`（PARTIAL 原地
    覆盖 + PARTIAL→FINAL 交接）；`FINAL event_id` = 确定性摘要（含 meetingId，
    因 `UNIQUE(event_id)` 全局）；PARTIAL event_id 混入 revision 内容，**不复用**该句
    固定 id。`asrSessionId` 上限 48 保证派生 id ≤ 64。
  - **Case A 不吞冲突**：同 event_id 但 canonical 内容不同 → **409**（沿用既有
    `_ensure_identical`，未改语义）。
  - **speakerId = -1 合法**（说话人暂未知）→ 内部 `unknown`，绝不产生 `speaker_-1`。
  - **未落库 metadata（limitation）**：`provider` / `asrSessionId` / `sentenceId` 只
    存活于 ingress 生命周期，**不新增 DB 列**；内部不硬编码腾讯（provider 不透明）。
  - Mock Producer 改为模拟**协作者格式输出**（camelCase、sentenceId、speakerId=-1、
    多段 PARTIAL、不发 segmentSeq），仍是纯外部 HTTP 客户端。
  - **PENDING（未单方面锁死）**：① `STREAM_END` 双方契约（谁发/何时/payload/
    `last_segment_seq` 归属）——继续用内部 mock 仅供回归；② multi-ASR-session
    时间轴（重连时 startMs/endMs 是否重置）。
  - **FUTURE**：multi-worker / MySQL 阶段，seq 分配须升级为数据库级安全机制
    （现为进程内 `RLock`，仅 MVP 单进程成立）。
  - **未改**：DB schema / 新 UNIQUE / migration / LLM Provider / DeepSeek /
    Meeting 状态机 / Freeze 语义 / 既有 ingress / 前端业务代码。
  - 测试：`scripts/test_asr_adapter.py` **ASR-A01~A20 20/20 PASS**；
    `scripts/e2e_asr_browser.py` 真实 Chrome CDP **14/14 PASS**（named SSE 真实到达、
    PARTIAL 原地覆盖、FINAL 替换、刷新 Snapshot 恢复、Summary 渲染、Regenerate v2）；
    回归 Step5 21 + Step6 21 + Step7A 28 + Step7B 36 + list 17 全绿；
    前端 typecheck + build PASS。


## 常用命令

```bash
# 后端
cd backend
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000

# 依赖
cd backend && .venv/Scripts/python -m pip install -r requirements.txt

# 前端
cd frontend
npm install
npm run dev        # Vite dev server on :5173（proxy /api -> :8000）
npm run typecheck  # tsc --noEmit
npm run build      # tsc --noEmit && vite build
```

## 文档索引

- `prd.md` — 产品 / 技术契约 / 状态机 / 验收
- `fronted_designer.md` — 前端设计
- `docs/architecture.md` — 架构与原则
- `docs/api-contract.md` — API / SSE / Transcript 契约（与实际实现一致）
- `docs/decisions/ADR-001-mvp-architecture.md` — MVP 架构决策
- **Step ASR-0B 已完成**：ASR Event Contract Freeze + E2E Acceptance。
  - **定位**：ASR-0A 让系统"能吃下"协作者输入；ASR-0B 把口头约定变成**有回归保护的
    冻结契约**。不接真实 ASR（V0.2），不改 MVP 产品范围。
  - **两个 PENDING 处置**：
    - **PENDING 1（STREAM_END）部分冻结**：`last_segment_seq` 归属已定（Transcript-owned，
      改**可选** + `ge=0`，省略时后端派生 `max(seq) or 0`，提供时保留完整 gap/overrun 校验）；
      **发送者身份与时机仍 PENDING**，MVP 由内部 mock 发（仅供 E2E，代码已标注非最终契约）。
    - **PENDING 2（multi-ASR-session 时间轴）✅ 冻结**：`startMs/endMs` = **session-local
      相对偏移**，跨 session **不可比较**；排序**永远只认 `segment_seq`**；MVP **不做 rebase**。
      ⏳ V0.2 tech debt：会议级全局时间轴需先持久化 session 元数据（当前不落库）。
  - **五个冻结决策**：① `last_segment_seq` 可选 + `ge=0` + 服务端派生；② `segment_seq`
    继续 Transcript-owned，上游不必反向查询；③ 时间轴如上；④ **utterance 关闭**——同
    utterance 一旦 FINAL 即 closed，后续 PARTIAL → **409** 且不进 live cache
    （`transcript_service._ensure_utterance_open`，**领域级**，两个 ingress 同时生效）；
    ⑤ `asrSessionId` maxLength=**48** 写入正式协议 + OpenAPI schema。
  - **修复真实缺陷：空会议永久卡死**。创建 → 开始 → 直接「结束会议」→ 因
    `Field(default=None, ge=1)` + 显式 `None → 400`，空会议 `0 → 422`、`1 → 409 missing [1]`，
    **永久停留 FINALIZING**、无出口。修复：`ge=1 → ge=0` + 字段可选 + 派生 `max_seq or 0`
    （`_find_gaps(last=0)` 为空，完整性算法本身无需改动）。**边界**：显式提供值时
    gap/overrun 校验原样保留，这不是放宽校验。
  - **`protocols/*.md` 升格为协议事实源**（原为 5 个 3 行占位）：README（优先级/变更流程/
    三条铁律）+ `transcript-format.md` ✅ + `realtime-events.md` ✅ + `meeting-api.md` ✅
    （均 FROZEN）+ `esp32-audio.md` ⏸（V0.2，列出开工前必答 6 问）。
  - **Mock Producer 场景覆盖**（`--scenario`）：`happy` / `reconnect` / `duplicate` /
    `out-of-order` / `errors`（9 项错误矩阵断言）/ `all`。仍是纯外部 HTTP 客户端，
    **仅依赖标准库**（有回归断言保证不 import Backend 内部）。
  - **internal ASR 独立 OpenAPI**：`GET /api/internal/docs`（Swagger UI）+
    `GET /api/internal/openapi.json`，**从真实路由派生**（同一 router 构造临时 app）
    故**不会漂移**；主 `/docs` 不变，此为附加视图。
  - **E2E 验收链路**：`mock producer（子进程，真网络）→ asr-events ingress → Transcript
    Domain → Snapshot → SSE（具名帧）→ Finalizer → Summary COMPLETED`，用**真实 uvicorn
    子进程 + 临时 SQLite**（TestClient 绕开网络与 socket，无法证明 SSE 帧真的到达）。
    ⚠️ 子进程 stdout **必须重定向到文件**，不可 `subprocess.PIPE`（access log 填满
    未读管道会阻塞服务器，曾致整套测试挂起）。
  - **pytest 收口 + CI 准备**：pytest 9.1.1 入 `requirements-dev.txt`；`backend/tests/`
    （`conftest.py` 每测试新建 schema；env 必须在任何 `app.*` import **前**注入，因
    `settings` 是 import 期单例）+ `test_asr_contract_freeze.py`（35）+ 
    `test_asr_e2e_acceptance.py`（20）。**两套并存**：`scripts/test_*.py` 手写脚本仍是
    早前 Step 的权威回归记录，**不删除**；CI 跑 `pytest` + `legacy-suites` **两个 job**，
    避免早期回归被绿色 pytest 掩盖。`.github/workflows/backend-tests.yml`（Python 3.13，
    全程离线，无需真实密钥）。
  - 测试：**pytest 55/55 PASS**；`test_asr_adapter.py` ASR-A01~A20 20/20；
    回归全绿（Step5 21 + Step6 21 + Step7A 28 + Step7B 36 + list 17）。
  - **未改**：DB schema / 新 UNIQUE / migration / LLM Provider / DeepSeek /
    Meeting 状态机 / Freeze 语义（仅 STREAM_END「终值来源」放宽）/ 前端业务代码
    （本 Step 零前端改动）。
  - 明确不做：真实 Tencent ASR / 音频采集 / ESP32 / PCM / 腾讯签名 / 真实 LLM Provider。
- **Step AGENT-1 已完成**：Meeting State Agent（Streaming Transcript → 会议工作记忆）。
  - **定位**：`Meeting State ≠ Final Summary`。State 是会议进行中的**工作记忆**
    （随 FINAL 增量更新）；Summary 是会后一次性的派生结果。**本 Step 不重构 Summary**，
    下一步（AGENT-2/后续）才考虑 `Meeting State → Final Summary Agent`。
  - **范围授权**：`Agent` 原属 PRD §3.2/§56 与 arch §10「MVP 不做」。本 Step 按用户
    **明确要求**实现，属授权范围变更，三处文档已同步标注（`prd.md §3.2` 变更记录 /
    arch §10 变更记录 / arch §19）。其余各项（RAG / Memory / Multi-Agent / 向量库 /
    跨会议问答 / LangGraph 等）**仍然不做**。
  - **链路**：`ASR → Transcript Domain →（FINAL 首次提交）FinalApplied → 应用层
    BackgroundTasks → Meeting State Consumer → Batch Buffer → LLM（Previous State +
    New Batch）→ Pydantic 校验 → meeting_state upsert`。
  - **三条边界（硬约束）**：① `asr_adapter` / `asr_service` **零 Agent 调用**
    （禁止 `save_transcript(); call_agent()`）；② Agent **只读持久化 FINAL**
    （`meeting_id`/`segment_seq`/`text`/speaker），**绝不**碰 provider raw payload /
    `asrSessionId` / `sentenceId`；③ Agent 是**下游消费者**，FINAL 落库 + SSE 广播
    在它被调度**之前**已完成 —— Agent 失败**绝不**阻塞 Transcript。
  - **幂等（§4）**：游标 `last_processed_segment_seq`，只处理 `seq > cursor`。
    游标与 `state_json` **同一次 commit** 写入，且**只在成功时推进**。
    `FinalApplied` 机制保证幂等重试**不调度**（不重付模型成本）；并发 run 在 Phase C
    锁内重读游标，不一致 → `SUPERSEDED` 丢弃本次结果。
  - **Batch/Flush（§5）**：`select_batches` 返回 `(ready, remainder)`；**只有达阈值**
    （`AGENT_BATCH_MAX_SEGMENTS` / `MAX_CHARS`）的批次才值得一次 LLM 调用，
    **未达阈值的尾部继续缓冲**（提前 flush = 每句一次调用，正是要避免的）。
    单次 run 上限 `AGENT_MAX_BATCHES_PER_RUN`。**无定时器 / 无后台线程**。
  - **强制 Flush（§11）**：`POST /finish` → `force_flush=True`，用 `_chunk()` 把
    **含尾部 remainder** 的全部 pending 按**同样阈值切分**后处理 —— **只解除批次数量
    上限，不并成一条超大 prompt**（否则会被尺寸守卫拒绝，反而丢掉最该抢救的尾句）。
  - **Persistence（§9）**：新增 `meeting_state` 表（**`create_all` 建表，无 ALTER /
    无 migration**）：`meeting_id PK / version / last_processed_segment_seq /
    state_json / status(EMPTY·ACTIVE·FAILED·FROZEN) / model_name / last_error /
    timestamps`。**必须新表**：既有三表语义均不兼容「单行 · 原地更新 · 会中可用」
    （meeting=四态生命周期；transcript_segment=append-only 事实；
    meeting_summary=append-only 版本化终稿）。
  - **Failure Policy（§18）**：失败 → `mark_failed` 记录 `last_error`，
    **游标不推进 + `state_json` 不动 + version 不增长** → 下次触发**重放**该批。
    Provider 级失败（超时/5xx/未配置/未知 provider）**不 repair**；仅形状级
    （JSON/Pydantic）走有界 repair。**无静默截断**（超硬上界整批拒绝）。
    `run_agent_background` **吞掉一切异常**，绝不让下游反噬转写路径。
  - **Feature Flag（§17）**：`MEETING_AGENT_ENABLED`（默认 true）；置 false 后
    ASR / Transcript / SSE / 既有 Summary **行为与变更前完全一致**，
    Agent 不构成新的单点故障。
  - **Observability（§15）**：每次 run 一行结构化日志（`meeting_id` / `agent_run_id` /
    `from..to_segment_seq` / `previous..new_state_version` / `llm_latency_ms` /
    `status`）；**绝不输出 transcript 原文**。
  - **配置（§16）**：全部集中在 `app/config.py`（无散落 `os.getenv()`）：
    `MEETING_AGENT_ENABLED` / `_BATCH_MAX_SEGMENTS`(8) / `_BATCH_MAX_CHARS`(1200) /
    `_MAX_ATTEMPTS`(2) / `_PARSE_REPAIR`(true) / `_MAX_BATCH_CHARS`(12000) /
    `_MAX_STATE_CHARS`(24000) / `_MAX_BATCHES_PER_RUN`(4)。
    **刻意不提供 `MEETING_AGENT_MODEL`**（模型名归 Provider 层，独立开关会漂移）。
  - **Debug API（§21）**：`GET /api/internal/meetings/{id}/state`（空状态 200 /
    会议缺失 404）+ `POST .../state/process-pending`（后台 drain，幂等）。
    **仅内部调试，非产品 UI；前端不使用**。
  - **明确不做（§19）**：真实腾讯 ASR / ESP32 / PCM / MQ / Kafka / Redis Stream /
    Celery / Multi-Agent 编排 / Planning Agent / Critic Agent / RAG / 向量库 /
    自动执行任务 / 邮件 / Calendar / 声纹映射 / 会议级时间轴 rebase / **Summary 重构** /
    **前端大改**。
  - **Known Limitations**：① **multi-worker 不安全**（游标推进依赖进程内
    `meeting_lock`，与 Step 5/6 同一限制；升级路径 = DB 行锁或
    `UPDATE ... WHERE version = :expected`）；② merge 策略**只能增改、不能删**
    （列表语义「非空替换、空/缺省保留」，安全方向；模型无法主动删除/编辑既有条目，
    属 AGENT-2）；③ 状态**可能滞后**于 Transcript（阈值设计使然）；
    ④ `source_segment_seqs` 只保证字段存在，**不保证引用正确**。
  - 测试：`backend/tests/test_meeting_state_agent.py` **40/40 PASS**
    （含 §13 确定性 6 段脚本会议回放：schema 有效 / 游标 / 去重重放不重复 /
    批次边界 / finish 强制 flush / 非法输出不污染 / source trace）；
    `pytest` **95/95 PASS**；回归全绿（Step5 21 + Step6 21 + Step7A 28 + Step7B 36 +
    list 17 + ASR-A 22）；前端 typecheck + build PASS。
  - **零改动**：Meeting 四态机 / Freeze 语义 / Transcript 契约（ASR-0A·0B）/
    LLM Provider / DeepSeek / **DB 既有表**（仅新增 1 张表，无 migration）/
    **前端业务代码**。
  - **本 Step 未 push**（按用户要求：可 commit 到 `dafenqi`，等待确认后再 push）。
