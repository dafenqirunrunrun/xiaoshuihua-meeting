# ADR-002: Captain Sidecar Architecture

- 状态：**Accepted**
- 日期：2026-09-23
- 作者：小水华会议助手 MVP 项目组
- 关联：`prd.md`、`docs/architecture.md`、`docs/api-contract.md` §1b、
  `docs/decisions/ADR-001-mvp-architecture.md`
- 实现：`backend/app/captain/`、`backend/app/demo/`
- 验证：`backend/tests/test_captain.py`（57）、
  `backend/tests/test_captain_scenarios.py`（14）、
  `backend/tests/test_demo_ingress.py`（18）

> 本文档记录**已经落盘并被测试钉住**的架构决策，不是设计方案。所有"决策"条目
> 均可由当前代码或测试直接核对。**证据优先于意图**：与本文件描述的架构不一致的，
> 以代码为准，并应视为待修复的缺陷。

---

## Status

**Accepted**

已实现并验证的范围：

| 阶段 | 内容 | 状态 |
|---|---|---|
| Phase 1 | 后端闭环（触发 → 分析 → 落库 → 读取） | 已完成 |
| Phase 2-A | SSE 通路（`captain.completed` / `captain.failed`） | 已完成 |
| Phase 2-B | 分析链路验收矩阵 | 已完成 |
| Phase 2-C | 场景评估（6 个经典会议场景） | 已完成 |
| Phase 2-D | 产品化接入（裁判面板 + 演示输入 + 快捷发送按钮） | 已完成 |
| 未实现 | 像素角色 / TTS | 未开始 |

Phase 2-D 的前端部分（`CaptainPanel` / `DemoComposer` / `useCaptainReferee` /
`useMeetingStream` 的 `captain.*` 分支）是**产品表现层**，不改变本 ADR 的任何
结构决策：面板一律回源 `GET .../captain/{report_id}` 渲染，SSE 只是失效通知
（即便 `captain.completed` 例外地携带了报告子集，也**不**用它渲染 —— 子集缺
`discussion_summary` / `agreement_points` / `evidence` / `suggestion`，
用它渲染会得到一份看起来完整、实则残缺的报告）。

**未实现的 `window_to_seq` 语义债**：该字段存的是**触发那一句（锚点）**，
而锚点按设计总被排除在分析内容之外。字段名沿用了「窗口上界」的直觉，
容易读成「分析了第 X–Y 条」。目前 UI 如实写作「第 X 条起（触发的那一句
不计入）」，并已在 `docs/api-contract.md` §1b 记录。改名属破坏性变更，
留待与其它内部字段一起清理。

---

## Context

### 需求

甲方提出「AI 裁判」需求。实际目标**不是评分裁判**，而是：在会议讨论出现争议、
僵持或信息不足时，AI 作为一个虚拟会议成员参与，提供决策辅助。

产品形态收敛为「水华队长」：

- 用户通过**唤醒词主动请求**裁判分析（说「水华队长」或点击按钮）
- Captain 分析**最近的会议窗口**（默认最近 3 分钟）
- Captain 输出**观点、分歧、不确定性和建议**
- **不应该影响实时会议分析链路**

### 关键约束：不能成为主链路

系统已稳定运行以下链路，且**不可被破坏**：

```text
ESP32 → 实时 ASR → Transcript Segment → Meeting Intelligence → Summary / Todo / Decision
```

在 Captain 之前，系统中已经存在一个**实时分析 Agent（Meeting State）**，负责持续
理解会议状态。Captain **不是**替代它，也**不是**它的扩展。

### 一个必须先说清的事实：两条链路的信号性质相反

这是本 ADR 最核心的背景。Meeting State 与 Captain 的**触发信号性质完全相反**，
这决定了它们必须使用不同的调度机制：

| | Meeting State（实时 Agent） | Captain（水华队长） |
|---|---|---|
| 信号来源 | **系统驱动**：每个 FINAL | **用户驱动**：唤醒词 / 按钮 |
| 频率 | 高频（每分钟数十次） | 极低频（每场会议数次） |
| 需要的机制 | **合并调度器**（窗口阈值 / 静默检测 / 突发检测 / 待处理高水位） | **触发派发**（队列 / 去重 / 节流 / 单 worker） |
| 合并两次信号 | 正确（同一批内容） | **错误**（会把 30 秒前的提问当成本次提问来回答） |

因此 Captain **没有** scheduler，也**不复用** `meeting_state_scheduler`。见
`captain/worker.py` 的模块文档：*"THIS IS NOT A SCHEDULER. The distinction is the
whole point of this module."*

---

## Decision

### 采用 Sidecar（旁路）架构

两条链路**共享 transcript 事实源，但互不调用**：

```text
Meeting Pipeline（既有，未改动）
    ASR → Transcript → Meeting Agent（Meeting State）→ Final Summary

Captain Pipeline（新增，旁路）
    FINAL transcript
      → wake word trigger
      → Captain Worker            （独立 daemon 线程 + 有界队列）
      → window selection          （captain/context.py）
      → LLM analysis              （captain/prompts.py，经 LLMProvider）
      → evidence validation       （captain/evidence.py，纯函数）
      → Captain Report            （captain_report 表，COMPLETED / FAILED）
```

### 明确的架构约束

| 约束 | 落地方式 |
|---|---|
| **Captain 独立运行** | 自有表 `captain_report`、自有 worker、自有 API 前缀、自有异常；不共享 `meeting_agent_run_lock` |
| **只读 transcript** | 仅调用 `transcript_repository.list_sorted` / `max_seq`，二者均为纯读（无 `add` / `commit`） |
| **不修改 meeting_state** | `app/captain/` 不 import `app.meeting_state`，也不 import `app.agent.summary*`（静态断言见 Evidence） |
| **不修改 transcript** | 无 INSERT / UPDATE / DELETE；不新增 transcript 列，不改 transcript schema |
| **不依赖 speaker attribution** | schema 中**没有任何承载人名或身份的字段**；`ASR_SPEAKER_LABELS_ENABLED=false` 时 `speaker_id` 恒为 `-1`，身份不可知 |

### 触发接入点

Captain 的触发挂在 `transcript/service.py::handle_event` 提交 FINAL **之后**的
**单一咽喉点**，而不是 ASR 适配器里。理由是系统存在**两条** FINAL 提交路径
（HTTP ingress 与真实 ASR 回调），二者**不共享** FastAPI `BackgroundTasks`。
若把触发挂在 `_schedule_agent`，语音触发会在真实 ESP32 硬件上**静默失效**
（mock / 开发环境却正常）。

该调用点**不 gate 在 `background_tasks` 上**，且异常**全部吞掉**：Captain 必须
永远无法破坏 transcript 持久化或 SSE 广播。

**第三条 FINAL 提交路径：产品级演示输入。** 产品需要在会议室页面让人打字模拟
多人对话（没有 ESP32 / ASR 在场时唯一的真实验证方式）。它**不**复用
`/transcript-events`（那是 producer-owned `segment_seq`，让浏览器调它等于把
transcript ordering 的所有权交给前端，违反 CLAUDE.md「segment_seq 由 Backend
生成」），也**不**复用协作方的 `/asr-events`（`asrSessionId` / `sentenceId` 是
别人的语义）。它新增 `app/demo/`：一个极薄的 `speaker + text` 契约，内部构造
`AsrTranscriptEvent` 后原封不动交给**既有**的 `asr_service.ingest_asr_event()`。

因此它**不是第三条实现**，只是第三个入口：ordering / 幂等 / SSE / Meeting State
调度 / 唤醒检测全部走同一条代码，没有可漂移的第二份逻辑。边界同样明确 ——
`app/demo/` 不写任何业务表、不改 transcript schema、不 import captain 的
service / worker / schemas（只用纯函数 `wake_word`）。

### 观点分析模型

**不预设两个立场。** Schema 用 `positions[1..5]` 数组，由模型从转写中自主识别
1~N 个观点簇。

```text
positions: [{label, statement, source_segment_seqs}]   ← 1..5，模型决定数量
```

**明确禁止 `side_a` / `side_b`。** 这不是命名偏好，而是两点硬约束：

1. 产品说话人标签默认关闭，`speaker_id` 不可靠。「A 方 / B 方」会诱导模型把主张
   归因到它**无法识别**的身份上，在裁判类产品里等于制造错误的归因。
2. 会议不是辩论。强行两个立场会在讨论是探索性、单边或已经一致时**虚构冲突**。

**`dispute_points` 允许为空**，且 schema **不设下界** —— 「不强行制造冲突」是
**结构性保证**，不是 prompt 里的一句礼貌请求。只有一种立场 + 空分歧列表是
**完全正常且常见**的结果。

### Report Schema

```text
discussion_summary   str
positions            list[Position]{label, statement, source_segment_seqs}   1..5
agreement_points     list[PointItem]{text, source_segment_seqs}              默认 []
dispute_points       list[PointItem]{text, source_segment_seqs}              默认 []
evidence             list[EvidenceItem]{segment_seq, quote, supports}        默认 []
uncertainty          str   必填且非空
suggestion           str
```

全字段 `extra="forbid"`。

`uncertainty` **必填且非空**：一份无法说明自己不知道什么的报告，不是裁判的报告。
prompt 为它提供内容（窗口范围、缺失事实、截断情况），校验拒绝空值。

### 证据校验是纯函数，不是第二次模型调用

`captain/evidence.py::validate_evidence()` 是**确定性**的：报告内容进，报告内容出，
无 DB、无 LLM、无 settings。

- 引用的 `segment_seq` 必须落在窗口内 —— 窗口外引用被剔除
- `evidence.quote` 必须是所引段落的**逐字子串**（宽松匹配空白与 ASR 标点差异）
  —— 伪造引文被**整条剔除**，绝不"改写为意译后保留"
- 悬空的 `source_segment_seqs` 被修剪

剔除**不静默**：剔除数量汇总进报告的 `uncertainty`，用户被告知本次依据有所缺失。
**刻意不做**：判断引文是否真的**支持**其主张 —— 那是判断，不是事实核查，需要
另一个模型，属 V0 范围之外。

### 失败与生命周期

```text
PENDING → ANALYZING → COMPLETED   （终态，永不覆盖）
                    ↘ FAILED      （终态，永不覆盖）
```

- **commit-before-publish**（ADR-001 D4 的延伸）：先落库，再广播 SSE。
- 失败**绝不留下半份报告**：只移动 `status` / `last_error`，`report_json` 保持 NULL。
  读者要么看到完整报告，要么看到诚实的失败。
- 模型输出形状错误 → **有界修复**（`CAPTAIN_MAX_ATTEMPTS`）；**provider 级错误不重试**
  （重试坏传输只是烧掉延迟，还把诚实的 FAILED 推迟）。
- 材料不足 → 在**调用 LLM 之前**拒绝（`CAPTAIN_MIN_SEGMENTS`），不花模型成本。

---

## Rationale

### 1. 实时链路与分析链路的 SLA 不同

```text
实时（Meeting State）    低延迟、高频、可合并、可容忍近似
Captain                低频、单次、高质量推理、不可合并
```

把二者塞进同一个调度器，必然要用一套阈值同时满足两种相反的负载特征：要么实时
链路被裁定延迟拖累，要么 Captain 被合并逻辑答错问题。**分离是 SLA 的函数，不是
代码组织的偏好。**

### 2. 故障隔离

Captain 失败**不能影响会议流程**。这在实现上是**结构性**的，而非靠约定：

- 触发点异常全部吞掉并记日志 → transcript 持久化与 SSE 广播不受影响
- 独立 worker + 独立队列 → 队列满时**响亮拒绝**（503），绝不静默丢弃
- LLM 调用运行在**两段短临界区之间**，绝不持有 `meeting_lock` → FINAL 提交不被
  LLM 阻塞（ADR-002 §10 的唯一硬禁止）
- publish 失败不回滚报告，也不把报告记为 FAILED（SSE 非事实源）

### 3. 独立演进

Sidecar 边界让以下工作可以**单独**推进而无需触碰主链路：

- judge model 替换（经 `LLMProvider`，已支持 provider 抽象）
- evaluation（Phase 2-C 已建立场景化评估基线）
- recommendation / suggestion 能力增强
- 前端表现层（**Phase 2-D 已落地基础形态**：裁判面板、演示输入、快捷发送按钮；
  动画 / 像素角色 / TTS 仍未开始）

---

## Non-goals

Captain **不负责**：

- **实时逐句裁判** —— 它是**按需触发**的，不主动持续监听，不参与会议讨论
- **speaker 身份判断** —— 身份在本系统中不可知（见 Decision）
- **修改会议状态** —— Meeting 四态机（ADR-001 D1）不受影响；Captain 不写
  `meeting` / `meeting_state` / `meeting_summary` 任何一张表
- **自动判断谁正确** —— 不评分、不给胜负、不评价任何一方立场是否合理
- **替用户做决定** —— `suggestion` 只能说明"缺什么信息 / 下一步该确认什么"，
  不得给出结论性建议
- **Multi-Agent 编排** —— 不引入 Conflict Detector / Mediator / Evidence Agent
  等独立 Agent。原本会是独立 Agent 的职责，表达为**单一结构化输出上的规则**；
  唯一真正需要独立的**证据校验**是确定性函数，不是模型调用
- **RAG / 向量库** —— 不引入
- **多 worker / 分布式** —— 单进程 MVP，与 ADR-001 D8 一致

---

## Evidence

### Phase 2-B：架构隔离的验证

| 断言 | 验证方式 |
|---|---|
| **不读取 meeting_state** | `test_captain_package_does_not_import_meeting_state`：子进程静态断言，`import app.captain.service` 后 `sys.modules` 中不存在任何 `app.meeting_state*` |
| **不写 meeting_state** | `test_no_meeting_state_row_is_written`：跑完整裁判链路后 `meeting_state` 表仍为空 |
| **不影响实时分析 Agent** | `test_captain_works_with_meeting_state_agent_disabled`：`MEETING_AGENT_ENABLED=false` 时裁判照常工作 |
| **不修改 transcript** | `test_captain_failure_does_not_break_transcript`；且代码层仅调用两个只读 repository 方法 |
| **多观点支持** | `test_three_distinct_positions`、`test_six_positions_rejected`、`test_zero_positions_rejected` |
| **无人物归因** | `test_report_contains_no_person_attribution`（窗口**内确实含**人名，故这是模型的义务，必须在**输出端**断言） |
| **错误不泄露** | `test_provider_error_text_never_reaches_the_client`：`last_error` 只在 ORM 行上，产品投影不含该字段；SSE 的 `reason` 为固定短码 |

### Phase 2-C：场景评估（`tests/test_captain_scenarios.py`）

| 场景 | 验证目标 |
|---|---|
| `clear conflict` | 两个对立观点不被合并（`>=2`）；`dispute_points` 不被清空 |
| `multi position` | 三观点不被压成两方；`side_a`/`side_b` 在响应 JSON 全文与 schema 字段集中都不存在 |
| `no conflict` | 分工不被当成分歧；`dispute_points == []`；schema **无下界**（空列表是合法输入） |
| `insufficient evidence` | `uncertainty` 非空；契约中**没有任何字段能承载结论**（`decision`/`verdict`/`winner`/`score` 均不在字段集）；`Position` 只有 `label`/`statement`/`source_segment_seqs` |
| `opinion shift` | 转向段**在窗口内**且带原文；窗口**按时间正序**送达 |
| `evidence guard` | 伪造 quote / 窗口外 seq 一律出不去；全部剔除时**降级为 COMPLETED 并披露剔除**，而材料不足时 **FAILED 且不花 LLM** |

**评估的诚实边界**：离线测试使用 scripted provider，它**不推理**。因此 Phase 2-C
的断言只落在三类可验证事实上 ——（a）pipeline 强制的**结构保证**、（b）**喂给模型
的是哪些材料**（这是"能识别观点变化"的离线可验证前提）、（c）真实报告形状的
**回归钉**。真实模型的叙事质量在 `LLM_PROVIDER=fake` 下**不可验证，不作声明** ——
那是 `scripts/replay_real_provider*.py` 的职责。

Phase 2-C 同时做了**反向验证（mutation test）**：关闭 `evidence.py` 的逐字引文
检查 → 2 例变红；删除 `context.py` 的时间正序重排 → 1 例变红。证明测试具备
**失败能力**，而非恒绿的装饰。

### Phase 2-D：产品化接入（`tests/test_demo_ingress.py`）

新增的产品级入口必须证明自己**没有**夺走 Transcript Domain 的所有权：

| 断言 | 验证方式 |
|---|---|
| **序号由服务端分配** | `test_segment_seq_is_allocated_by_the_server_not_the_client`：请求体带 `segment_seq` → 422（`extra="forbid"`）；多轮发言返回 `[1,2,3,4]` |
| **与真实链路同构** | `test_demo_and_real_asr_paths_share_one_ordering`：演示发言与 `/asr-events` 混投，序号出自同一个 max+1 分配器 |
| **一条发言就是一条真实 FINAL** | `test_demo_utterance_becomes_a_real_final`：事后 `GET /transcript` 能读到，且 speaker 归一化与 ASR 路径一致（`speaker_0`） |
| **唤醒判定在服务端** | `test_wake_word_line_reports_triggered_captain` + `test_wake_word_line_actually_reaches_the_referee`：含唤醒词的发言经既有咽喉点真的产出 COMPLETED 报告 |
| **快捷按钮不持有唤醒词** | `test_quick_send_follows_a_reconfigured_wake_phrase`：改 `CAPTAIN_WAKE_WORDS` 后快捷发送跟着变 —— 若有人把唤醒词写回前端常量，此例变红 |
| **时间轴单调** | `test_timeline_is_monotonic_even_when_the_client_asks_to_go_backwards`：`start_ms` 被钳为上一条 `end_ms`，绝不接受倒流 |
| **失败隔离** | `test_demo_failure_does_not_disturb_the_transcript`：非法请求后已落库转写与后续提交完好无损 |
| **开关移除能力而非拒绝** | `test_disabled_demo_ingress_removes_the_route`：`DEMO_INGRESS_ENABLED=false` → 404（与 `captain_enabled` 的 503 语义刻意不同） |

### 汇总

```text
tests/test_captain.py             57 passed
tests/test_captain_scenarios.py   14 passed
全量 tests/                      350 passed（零回归）
  test_captain.py                 57
  test_captain_scenarios.py       14
  test_demo_ingress.py            18
```

---

## Consequences

### Positive

- **架构隔离**：主链路零改动。Captain 的引入对 `ASR → Transcript → Meeting
  Agent → Summary` 是**加法**，不是重构。
- **易测试**：确定性证据校验 + 只读窗口选择 + 可注入 provider，使全链路可离线、
  可复现地验证。
- **易扩展**：judge model / evaluation / recommendation / 前端表现层可各自独立演进，
  互不阻塞。
- **可恢复**：SSE 非 durable，但报告本身 durable。`captain.completed` 只是快路径
  （payload 直接携带内容以便即时渲染），`report_id` 始终在 payload 中，断线 /
  刷新 / 重连的客户端靠 `GET .../captain/{report_id}` 恢复。

### Trade-off

- **需要维护 transcript window**：Captain 每次分析都要重新读取并裁剪最近窗口。
  窗口选择策略（时间界 / 尺寸界 / 锚点排除 / 顺序保持）是需要持续维护的产品逻辑。
- **存在额外分析延迟**：一次裁判 = 一次完整 LLM 调用。以 202 + 轮询/SSE 的方式
  异步交付，而不是同步等待（同步会长时间占用单进程 HTTP worker，且刷新即丢失结果）。
- **窗口语义有边界情况**：锚点（触发词所在 FINAL）**总被排除**，故 N 段讨论至少
  需要 `N+2` 段会议（+1 锚点，且需满足 `MIN_SEGMENTS=2`）；锚点缺少 `start_ms`
  时退化为按最近片段选取，并如实披露时间精度下降。
- **非 durable 的 SSE 事件**：`captain.*` 不随 `after_segment_seq` 回放。这是与
  `summary.*` / `meeting.state.updated` 一致的既定语义，代价是客户端必须实现
  恢复读取。
- **一处刻意的协议偏离**：`meeting.state.updated` / `summary.*` 是纯失效通知
  （payload 仅标量），而 `captain.completed` **例外地**携带报告内容。安全性由
  payload 中的 `report_id` 兜底。此偏离已在 `sse_service.py` docstring、
  前端两处注释与 `docs/api-contract.md` §1b 三处显式记录。

---

## 明确禁止事项

以下事项违反本 ADR，**不得**在未修改本文件的前提下引入：

1. **禁止**让 `app/captain/` import `app.meeting_state`，或反向引用
2. **禁止** Captain 写 `meeting` / `meeting_state` / `meeting_summary` / `transcript_*` 任何表
3. **禁止**修改 transcript schema 以适配 Captain
4. **禁止**在持有 `meeting_lock` 期间调用 LLM
5. **禁止**引入 `side_a` / `side_b` 或任何形式的两方预设
6. **禁止**在 schema 或输出中承载 speaker 身份、人名或职位
7. **禁止**让 `dispute_points` 出现结构性下界（那会迫使模型虚构冲突）
8. **禁止**允许空 `uncertainty`
9. **禁止**伪造或"意译后保留"无法核对的引文
10. **禁止**让 Captain 的失败传播到 transcript 持久化、SSE 广播或 Meeting 状态
11. **禁止**为 Captain 复用 `meeting_state_scheduler` 的合并逻辑
12. **禁止**引入 Multi-Agent 编排 / RAG / 向量库（与 `architecture.md` §19.11 /
    §20.11 的既有排除清单一致）
13. **禁止**静默丢弃触发（队列满必须响亮拒绝并标记 FAILED，且广播 `captain.failed`）
