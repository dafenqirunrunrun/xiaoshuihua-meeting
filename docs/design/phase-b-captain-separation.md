# Phase B/C：实时 AI 会议理解 与 水华队长 的职责分离（交付报告）

- 日期：2026-09-29
- 范围：**仅本地 `D:\interviewer agent`**。未触碰任何服务器、
  `meeting-management`、生产 `.env` / secret。
- 关联：`docs/decisions/ADR-002-captain-sidecar-architecture.md`、
  `ADR-003-captain-external-evidence.md`、`docs/api-contract.md`
- 验证：后端 **489** 测试全绿；`npm run typecheck` / `npm run build` 通过。

> 本文是 **Phase B/C 的交付报告**，不是设计草案。所有数字均来自本仓库的真实
> 运行（离线套件 + 一次真实模型端到端），并注明取数方式。

---

## 1. 为什么这两个能力会被混淆

三条**结构性**原因，都不是"prompt 写得不好"：

1. **同一个事实源**。`meeting_state`（实时理解）与 `captain`（裁判）都只读同一份
   已提交的 FINAL transcript（ADR-002 的旁路架构）。共同输入 + 相似模型 =
   倾向于相同输出，而"会议摘要"是两者最容易共同收敛到的形态。
2. **输出形状没有互相排斥的字段**。旧裁判报告 `CaptainReportContent` 里
   `discussion_summary` 是必填且允许很长；实时理解的核心产物也是"现在在讨论
   什么"。两者在 schema 层面**都能装下同一段话**，于是模型就都写了。
3. **触发时机让人误以为裁判是"再来一遍摘要"**。按钮/唤醒词触发后，用户先看到
   实时理解的摘要，紧接着看到裁判报告的第一段又是摘要 —— 体验上就是重复。

**结论**：分离必须是 **schema 层 + prompt 层**同时收口，只改一边会在另一边漏回来。

---

## 2. 变更文件清单

### 后端（新增）

| 文件 | 作用 |
|---|---|
| `backend/app/captain/search/research.py` | ResearchRouter：纯规则，先决定"问什么、能不能问"，再交给 provider |
| `backend/tests/test_captain_quick_delivery.py` | Quick 先于 Deep 到达 + 延迟埋点 + 搜索失败不拖垮两阶段 |
| `backend/tests/test_captain_evidence_layers.py` | 三层信息优先级在 **prompt 文本**上被声明 |
| `backend/tests/test_captain_research.py` | §7 研究路由：具体问题 / 六类禁止项 / 来源层级 |

### 后端（修改）

| 文件 | 改动 |
|---|---|
| `backend/app/captain/schemas.py` | `QuickAssessment` / `DeepAssessment`；`CaptainReportContent.quick/.deep` 可选；`ExternalEvidence.source_quality/.answers_question` |
| `backend/app/captain/prompts.py` | `build_quick_prompt` / `build_deep_prompt`；`_EVIDENCE_PRIORITY_RULES`(P1–P4)；`_render_deep_rules()` 的 rule 1 二选一 |
| `backend/app/captain/service.py` | `_generate_two_stage`（含 `on_quick` 回调）；`_publish_quick`；`_safe_enrich`（调用方兜底）；`_created_at_epoch`；`_log_metrics` 增加四个时间戳与派生毫秒 |
| `backend/app/captain/search/service.py` | 用 ResearchRouter 产出查询；`_stamp()` 赋 `source_quality` / `answers_question`（冷/热路径都赋） |
| `backend/app/captain/search/schemas.py` | `SearchResult` 两个流水线字段；`SearchMetrics` 四个 §7 标记 |
| `backend/app/captain/external_evidence.py` | 透传两个新字段；`render_for_prompt` 输出 `source_quality` / `answers_claim` |
| `backend/app/realtime/schemas.py` | 新增 `captain.quick_assessment`（含理由注释） |
| `backend/app/realtime/sse_service.py` | `build_/publish_captain_quick_assessment` |
| `backend/tests/test_role_separation.py` | 新增两阶段 prompt 对的职责边界覆盖 |
| `backend/.env.example` | `CAPTAIN_TWO_STAGE_ENABLED` 说明 |
| `backend/tests/conftest.py` | （本次尝试后又回退：默认值保持 OFF，见 §6） |

### 前端（修改）

| 文件 | 改动 |
|---|---|
| `frontend/src/types/sse.ts` | `SseEventType` 新增 `captain.quick_assessment` |
| `frontend/src/utils/sse.ts` | 白名单新增同名（穷尽性守卫） |
| `frontend/src/types/captain.ts` | `CaptainQuickAssessment` / `CaptainDeepAssessment` / `CaptainExternalEvidence` / `CaptainEvidenceStrength` / `CaptainAssuranceGap` |
| `frontend/src/hooks/useMeetingStream.ts` | `onCaptainQuick` 回调（**不停止** loading，独立于 `onCaptainEvent`） |
| `frontend/src/hooks/useCaptainReferee.ts` | `quick` state + `acceptQuick`；`report_id` 归属校验；终态清除 |
| `frontend/src/components/CaptainPanel.tsx` | `QuickProvisionalCard` / `DurableQuickBlock` / `DeepAssessmentBlock` / `ExternalEvidenceBlock` |
| `frontend/src/pages/MeetingRoom/MeetingRoomPage.tsx` | 接线 `onCaptainQuick`、传 `quick` |
| `frontend/src/styles/global.css` | 上述 block 的样式（复用既有 token） |

### 文档

`docs/api-contract.md`（`captain.quick_assessment` 契约与刻意偏离）、
`docs/decisions/ADR-003-captain-external-evidence.md`（ResearchRouter 小节 + 2 条技术债）

---

## 3. 两个 prompt 的最终职责

| | 实时 AI 会议理解（观察者） | 水华队长（裁判） |
|---|---|---|
| 回答 | **会议现在在哪** | **分歧是什么、依据有多强** |
| 输出 | 当前议题 / 已确定 / 未决 | 争议结构 + 证据分层 + 判断 |
| 明确禁止 | 判谁对谁错、评证据强弱、给结论、主动找反证、给决策建议 | 重新生成完整会议摘要、人物归因、百分比、替用户做决定 |
| 关键短语 | 「你是会议实时状态维护器，不是会议裁判」 | 「你不是会议记录员」「禁止重新生成完整的会议摘要」 |
| 阶段 | 单段（增量） | **Quick**（4 字段，schema 上写不长）+ **Deep**（完整推理） |

Deep 的 16 条规则把"必须给的判断材料"写死为字段名：`claims` /
`evidence` / `counter_evidence` / `confirmed_facts` / `assumptions` /
`missing_evidence` / `evidence_strength` / `supersedes`。

---

## 4. 新数据流

```
触发（按钮 / 唤醒词）
  → 锁（短）：建 PENDING 行，report_id
  → 解锁
  → select_window()                      ← 原始 FINAL 片段 + segment_seq
  → SearchPolicy.decide()   （纯规则）
      └ SEARCH → ResearchRouter.plan()   （纯规则，§7）
            ├ internal_information_required → 不调 provider，打标
            └ questions[2..3] → provider.search()（硬 900ms）
      → to_external_evidence()           ← 唯一转换点，喂 prompt + 写报告
  → Quick（无外部证据，独立小预算）
      → 解析成功 → **立刻** publish captain.quick_assessment（仍在 ANALYZING）
  → Deep（有外部证据）
      → 解析 → 派生扁平字段 → evidence.py 校验 → **覆盖** external_evidence
  → 锁（短）：mark_completed
  → publish captain.completed
  → 一行 captain_search_metrics 日志
```

**队长不喂摘要**：窗口是原始转写片段（带 `segment_seq`），不是实时理解的输出。
关键判断可逐条回溯到原句（`source_segment_seqs` / `segment_seq`），
伪造引文会被 `evidence.py` 整条剔除。

---

## 5. Quick / Deep 调用链与**实测**延迟

真实模型端到端（`LLM_PROVIDER=deepseek`，冻结 bad case 五行窗口）：

| 指标 | 实测 |
|---|---|
| `trigger_to_quick_ms` | **3864 ms** |
| `quick_ms`（Quick 单段） | 3864 ms |
| `deep_ms`（Deep 单段） | 14267 ms |
| `trigger_to_deep_ms` | **18131 ms** |

**如实说明**：`trigger_to_quick_ms` = 3864ms，**没有落在 1–2 秒目标内**。
原因是含队列等待 + 网关往返；`quick_ms` 本身就是 3864ms（`quick_started_at`
在调用前打点）。要压到 2s 内需要换更快的模型或更短的 Quick prompt，
属于未解决的问题（见 §10）。

延迟埋点字段：`trigger_at` / `quick_started_at` / `quick_completed_at` /
`deep_completed_at`，派生 `trigger_to_quick_ms` / `trigger_to_deep_ms`，
与 `quick_ms` / `deep_ms` 并存（前者含队列，后者纯模型段）。

---

## 6. 开关现状（重要，如实说明）

`captain_two_stage_enabled` **默认仍为 `false`**。理由：

- 单通道路径是**既有契约**（`CaptainReportContent` 扁平形状），`test_captain.py`
  (61)、`test_captain_scenarios.py` (14) 等按它写的；
- Phase 3-2E 搜索 A/B 的**基线臂**必须是旧行为（prompt 逐字节等价），
  开两阶段会同时改两处；
- 两阶段路径已有 **43** 个专属测试（quick_delivery 13 + two_stage 18 +
  evidence_layers 12），不是"未测"。

**产品化时**置 `CAPTAIN_TWO_STAGE_ENABLED=true`（`.env`，无需改代码）。
本次**尝试**过改为默认 true，导致约 18 个单通道测试失败（fake provider 返回
扁平形状，两阶段解析器拒收 → FAILED），因此**已回退**并把这个取舍写进
`config.py` 与 `.env.example`。这不是"没做完"，是刻意的兼容决定。

---

## 7. 坏案例（冻结回归）实测输出

窗口（五行八事实）：核心功能已完成 · 功能测试通过 · **压测尚未完成** ·
压测环境需三天 · 发布会下周五 · 用户量不到一万 · 历史事故根因涉连接池配置 ·
**连接池参数已修改**。

- 收敛于：**「上线前到底需要达到什么压力验证标准？」**（`suggestion` 与
  `deep.recommendation` 两处一致，`converges_n = 2`）
- **非**「双方仍有分歧，需要进一步讨论」
- 反证 12 个字段命中；显式引用原句 `「不过这个问题我们上周已经改了连接池参数」`
- `percent = 0`（无任何百分比）
- 证据强度：中 / 中 / 中高 / 中（四个等级，无分数）
- `supersedes` 非空且**解释了改了什么** —— 显式修正，非静默反转
- `evidence_validation.dropped_evidence = 0`

---

## 8. 关键事实召回（8 事实）

第一次评估脚本报了 **7/8**，缺 `functional_test_passed`。
**这是我的评测脚本假阴性，不是模型漏召回**：报告原文写的是
「核心功能已开发完毕，测试已跑过一轮」，确实召回但未含字面 token「功能测试」。

改为**谓词式**判定（`'功能测试' in t or ('测试' in t and ('跑过' in t or '通过' in t) and '压力测试' not in t)`，
逐字段判定而非整段裸 substring）后：

```
recall = 8/8   missing = []
```

八条事实的命中字段分布：`core_feature_done` / `functional_test_passed` /
`load_test_not_done` / `env_prep_three_days` → `agreement_points` +
`deep.confirmed_facts`；`launch_event_next_friday` → `discussion_summary` +
`position.statement`；`user_base_under_10k` → `discussion_summary` +
`deep.counter_evidence.quote`；`incident_root_cause_pool` → `discussion_summary` +
`suggestion`；**`pool_params_already_changed` → `discussion_summary` +
`agreement_points`**（不再漏）。

---

## 9. 测试与评测结果

| 项 | 结果 |
|---|---|
| 后端测试 | **489 passed, 0 failed** |
| 根因（真实缺陷）：搜索 provider 抛异常 → 报告 FAILED | 已修：`_safe_enrich` 调用方兜底 |
| `npm run typecheck` | 通过（exit 0） |
| `npm run build` | 通过 |
| 关键事实召回 | 8/8 |
| 反证召回 | 12 字段 / 1 条显式引用 |
| Groundedness | `dropped_evidence = 0`（引文逐字校验） |
| 角色分离 | prompt 层断言（单通道 + 两阶段两套 prompt） |
| Quick/Deep 一致性 | `supersedes` 非空且有解释 |
| 延迟埋点 | 四时间戳 + 两派生毫秒 |

评测覆盖的 7 项：Critical Evidence Recall / Counter Evidence Recall /
Groundedness / Role Separation / Quick-Deep Consistency / Latency — 均有对应
测试或真实模型取数。**未做**的：跨多次运行的分位数统计（单次实测，见 §10）。

---

## 10. 未解决的问题

1. **Quick 实测 3864ms，未达 1–2s 目标**。含队列与网关往返。需换更快模型 /
   缩短 prompt / 预热，或把目标改为"比 Deep 早 ~14s"（相对收益已达）。
2. **ResearchRouter 的问题措辞生硬**。真实窗口上产出
   「最新 不过这个问题我们上周已经改了连接池参数?」—— `_question_from_claim`
   保留了会话填充词。当前该窗口是 `INTERNAL_DECISION`，`enrich` 不搜索，
   plan 只用于埋点，**无功能损害**；但作为质量项待改（应剥掉口语前缀）。
3. **两阶段默认关闭**（§6）。产品化需显式开启。
4. **`source_quality` 只看域名**，不看内容 / 发布时间 / 交叉验证；同域名的官方
   文档与营销页同分。是给读者的线索，不是可信度结论。
5. **禁止项匹配是子串匹配**，双向都可能误判：贴边的内部术语可能漏拒，
   技术问题里带「成本」可能误拒。刻意偏向拒绝（误拒代价 < 误放代价）。
6. **Brave provider 未经真实响应验证**（ADR-003 既有技术债，Phase 3-2E 未开始）。
7. **前端无渲染测试**（项目无前端测试运行器，仅 typecheck/build 门禁）。
8. **`captain.quick_assessment` 是非 durable 事件**：断线重连的客户端收不到临时
   卡片，会直接等到 `captain.completed`。这是设计取舍（Quick 的价值是"早"），
   不是缺陷，但需知悉。
