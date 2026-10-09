# ADR-003: Captain External Evidence (可插拔 Web Search)

- 状态：**Accepted**
- 日期：2026-09-24
- 作者：小水华会议助手 MVP 项目组
- 关联：`docs/decisions/ADR-002-captain-sidecar-architecture.md`、
  `backend/app/captain/search/`、`docs/api-contract.md` §1b
- 实现：`backend/app/captain/search/`、`backend/app/captain/external_evidence.py`
- 验证：`backend/tests/test_captain_search.py`（38）

> 本文档记录**已经落盘并被测试钉住**的架构决策，不是设计方案。所有「决策」条目
> 均可由当前代码或测试直接核对。**证据优先于意图**：与本文件描述的架构不一致的，
> 以代码为准，并应视为待修复的缺陷。

---

## Status

**Accepted**

| 阶段 | 内容 | 状态 |
|---|---|---|
| Phase 3-2 | 搜索策略 / provider 抽象 / 降级 / 分层 / 缓存 / A/B 指标 | 已完成 |
| Phase C.5 | 与 Quick 解耦（检索排在 Quick **之后**）/ 证据字段强化 / stale run 保护 | 已完成 |
| Phase 3-2E | Brave vs Exa 实测 A/B 评估 | **未开始** |

### Phase C.5 决策：检索必须排在 Quick **之后**

Phase 3-2 把 `enrich` 放在两阶段分支**之前**——Quick 尚未产出，一次无界的外部
HTTP 请求已经压在「1–2 秒出第一段判断」的关键路径上。这是**顺序 bug**，不是
超时值不够：把 `captain_search_timeout_ms` 调到多少都无法让一个已经开始的网络
调用从 Quick 的路径上消失。

修复是结构性的：`_generate_two_stage` **不再接收**现成的 external evidence，改
接收一个 thunk（`enrich_fn`），并在 `on_quick`（第一段已交付客户端）**之后**才
调用它。Quick 的 prompt 因此**按构造**不含 `<external_evidence>` 块——不是
「约定不填」，而是根本没有值可填。回归断言在
`tests/test_captain_research_ordering.py`（8 条，断言的是**调用顺序**，因为
只看结果的断言在错误版本上同样通过）。

`captain_search_timeout_ms` 900 → 2500ms 是这次顺序修复的**后果**而非前提：
检索不再占用 Quick 的时间预算，只有 Deep 在等它，所以可以给真实往返留出余量。
该配置经核实**只**约束 provider 调用，不影响全局 LLM timeout，且仍可由
`CAPTAIN_SEARCH_TIMEOUT_MS` 覆盖。

前端侧同一缺陷的镜像（`captain.completed(A)` 与 `quick_assessment(B)` 落在两个
独立回调里，导致没有任何一方知道 A 已终态）由**单一入口** `handleCaptainEvent`
修复；归属判定抽成纯函数 `captainRunOwnership.ts`，由 `npm run test:hooks` 直接
断言，不依赖组件测试运行器。

**对外声明（重要）**：Brave provider 的响应解析
（`app/captain/search/brave.py`）

```
implementation based on current official Brave API docs;
live contract validation pending Phase 3-2E.
```

即：**当前阶段不声称「真实 Brave 已验证」**。契约测试
（`tests/test_captain_search.py::TestBraveContractFixture`）钉住的是一份
**按官方文档构造的合成 fixture**（`tests/fixtures/brave_llm_context_response.json`），
不是抓取的真实响应。Phase 3-2E 第一次真实调用必须：抓一份脱敏响应 →
与本解析器对比 → 有差异先修解析器 → 再开始压测。

---

## Context

### 需求

甲方希望「水华队长」在讨论出现**事实性分歧**时，能够查一下外部资料再作判断。
约束来自 Phase 3-2 规格的六条核心原则：

1. Captain 的最终 LLM 仍然只调用一次；
2. Search 是丰富手段，不是关键依赖；
3. Search 超时/失败**不得**导致 Captain 整体 FAILED；
4. 转录证据与外部证据必须严格分层；
5. 第一版不做 DeepResearch、不抓整页、不做多轮 ReAct；
6. Provider 必须可替换，以便 Brave / Exa A/B。

### 被否决的方案

| 方案 | 否决理由 |
|---|---|
| **LLM 路由**（先问模型要不要搜） | 两次 LLM 调用、两个失败面，而预算是 900ms。更关键的是它会误判：「周五上线还是下周上线」品味起来像事实问题，模型可能真的去搜，而答案只存在于团队自己的产能里。规则可以在代码上读出为什么不会犯这个错。 |
| **抓取整页** | §6 明确禁止。抓页引入第二个超时、第二个解析器、第二份不相关文本，与 900ms 预算不兼容。 |
| **外部证据进 `EvidenceItem`** | 网页不是转写片段。给它一个 `segment_seq` 就会让下游（包括引用校验器）把它当成会议内容。 |
| **Search 失败 → Report FAILED** | 把「丰富手段」变成「关键依赖」，与原则 2/3 直接冲突。 |
| **把指标写进 `captain_report` 表** | `create_all()` 不会 ALTER 已存在的 SQLite 表，新列在已部署库上会静默缺失；且这些是工程字段，混入 `report_json` 会让产品数据与 A/B 仪表无法分别演进。 |

---

## Decision

### 1. 目标链路

```
Captain Trigger
  → select_window()
  → SearchPolicy.decide()          ← 纯规则，不是第二次 LLM 调用
  → NO_SEARCH | SEARCH
  → WebSearchProvider.search()     ← 唯一网络调用，硬性 900ms 预算
  → to_external_evidence()         ← provider 结果 → 报告结构，唯一转换点
  → Captain LLM（仍然只调用一次）
  → transcript evidence 校验（captain/evidence.py，确定性）
  → external_evidence **覆盖写入**（绝不从模型输出解析）
  → CaptainReport
```

`enrich()` 是这条链上唯一的入口，且是**全函数**：禁用、NO_SEARCH、
超时、provider 错误、返回非法类型、provider 抛异常，全部返回一个正常的
`EnrichmentResult`。**它不可能让一份报告失败。**

### 2. SearchPolicy：四类判定 + 优先级

`app/captain/search/policy.py`，纯函数，输入 `[(segment_seq, text)]`
（没有 session、没有 ORM、没有 settings）。

| 优先级 | 类型 | 是否搜索 | 例子 |
|---|---|---|---|
| 1 | `USER_REQUESTED_VERIFY` | 是 | 「水华队长，帮我们查一下谁说得对」 |
| 2 | `INTERNAL_DECISION` | **否** | 「周五上线还是下周上线?」 |
| 3 | `CURRENT_FACT` | 是 | 「现在这个版本还支持 X 吗?」 |
| 4 | `FACTUAL` | 是 | 「鸡和蛋哪个先出现?」 |
| 5 | out-of-band | 视标记而定 | 强标记 + 疑问句式 + 可核对要素 |

两条 load-bearing 的护栏：

- **`_looks_like_claim_dispute` 要求「来自不同片段的 ≥2 个主张标记」**。
  一个人说「我认为…我认为…」是一个观点，不是分歧，不得花掉预算。
- **`_is_checkable` 要求存在数字或非纯观点标记**。
  「我认为应该先把功能做完整」是偏好，没有外部来源能裁定，直接不搜。

`MAX_QUERIES = 2`，且只有第一条查询**没有结果**时才会发第二条。

### 3. WebSearchProvider 抽象

```python
class WebSearchProvider(Protocol):
    name: str
    def search(self, query: str, *, max_results: int, timeout_ms: int) -> SearchResponse: ...
```

- **provider 问题返回 `SearchResponse`，不抛异常**——这样「无外部证据」在
  下游只有一条代码路径，无论原因是 NO_SEARCH、冷缓存、超时还是 500。
- `error_code` 是**短稳定 slug**（`timeout` / `http_500` / `invalid_json`），
  永远不是 provider 的消息或异常文本。provider 报错信息里可能带着含
  API key 的 URL。
- **Brave 特定结构只存在于 `brave.py` 内部**。`grounding` / `sources` 这些
  概念没有任何一处泄漏到 Captain 主逻辑。
- 工厂 `get_search_provider()` **fail-closed**：未知 `CAPTAIN_SEARCH_PROVIDER`
  **抛错**，不回落默认值——否则一次 A/B 会拿某个 provider 跟自己比。

### 4. 两份证据的边界

| | 转录证据 `EvidenceItem` | 外部证据 `ExternalEvidence` |
|---|---|---|
| 是否有 `segment_seq` | 有（`ge=1`） | **没有，且永远不会有** |
| 校验方式 | `captain/evidence.py` 逐字核对持久化片段 | **不可校验**——没有可比对的东西 |
| 权威来源 | 本系统（转录是事实源） | 搜索 provider |
| 防幻觉机制 | 对不上就剔除 | 条目由**流水线**从 provider 结果构造，模型无法生产 |
| 标识 | `segment_seq` | `source_id`（`web_1`..`web_n`，按位置连续） |

模型可以**引用** `[web_n]`，但**无法生成**它：`content.external_evidence`
在 `_generate()` 返回之后被 `to_external_evidence(enrichment.evidence)` **整体覆盖**。
一个幻觉出来的 `web_9` 无处可落。

**一次转换，两处使用**：`to_external_evidence()` 的输出同时喂给 prompt 和写入
报告。转换两次就可能重新编号，模型引用的 id 会静默指向另一个来源。

### 5. 降级：总是披露，从不静默

搜索失败时，一句固定的产品话术被折进报告的 `uncertainty`：

| 情况 | `note` |
|---|---|
| 超时 | 本次外部资料未能在时间预算内完成检索，以下判断主要依据会议内容。 |
| provider 错误 | 本次外部资料检索未能完成，以下判断主要依据会议内容。 |
| 未配置 | 外部资料检索服务当前不可用，以下判断主要依据会议内容。 |
| 无结果 | 外部资料检索未找到可用来源，以下判断主要依据会议内容。 |
| **功能关闭** | **空字符串** |

最后一行是刻意的：部署方主动关掉了这个功能，那么每份报告都写一句
「本次未检索」是噪音，不是诚实。

### 6. 缓存

进程内 TTL 缓存（`search/cache.py`），**刻意不用 Redis**：单进程旁路、
每场会议个位数查询，分布式缓存只会增加运维依赖和一次网络往返。

- key = `(provider, normalized_query, freshness_class)`。三部分都 load-bearing：
  **provider 名在 key 里是 A/B 的前提**——否则 Brave 的结果会被 Exa 那一臂读到，
  整场评估就变成「provider 跟自己做对比」。
- TTL：`STATIC_FACT` 24h / `TECH_FACT` 1h / `CURRENT_FACT` 5m。
- **超时、报错、空结果一律不缓存**。缓存一次超时会让降级在整个 TTL 内变得粘性。
- 超长时按「最早过期」淘汰，不是 LRU：这里的性质是过期顺序。

### 7. A/B 指标（§10）

每次分析输出**一行结构化 JSON 日志**（logger `app.captain`，前缀
`captain_search_metrics`），字段即 `SearchMetrics`：

```
search_triggered / search_policy_type / search_policy_reason / search_query_count
search_provider / search_latency_ms / search_timed_out / search_error_code
search_result_count / search_cache_hit / external_evidence_count
captain_llm_latency_ms / captain_total_latency_ms
```

`search_result_count` 与 `external_evidence_count` **刻意分开**：provider 可能返回
5 条而全部归一化失败或全部同一域名，合并这两个数会正好掩盖这种情况。
`search_latency_ms` **只统计 provider 时间**，缓存命中记 0——这是 3-2E 用来比较
Brave 与 Exa 的数字，不能被缓存和策略开销稀释。

**没有落库**（见「被否决的方案」）。

### 8. Prompt 等价性（A/B 基线的前提）

`build_report_prompt(external_evidence=...)` 在无外部证据时，输出与 Phase 3-2
之前**逐字节相同**；`<external_evidence>` 块与 A–G 补充规则只在有证据时出现。

这是 load-bearing 的：Phase 3-2E 要拿「开搜索」与**真正是旧行为**的基线对比，
而不是与一个同时改了两处的 prompt 对比。`tests/test_captain_search.py::
TestPromptEquivalence` 钉住了这一点。

---

## Consequences

### 已实现

- 搜索按需触发、可降级、可验证；关掉开关是**完整**回到 3-2 之前的行为。
- provider 可替换：新增 Exa 只需实现 Protocol + 在工厂注册一行，业务代码零改动。
- 两份证据在 schema 层面无法混淆。
- 38 个测试离线覆盖 7 个规定场景 + Brave 契约 fixture。
- **ResearchRouter（Phase 3-2 §7 补做）**：策略判定「要不要查」之后，再由
  `search/research.py` 决定「具体问什么」。规则仍是纯规则、无 LLM。

#### Phase 3-2 §7 补做：ResearchRouter

原实现把 policy 产出的查询串（两条主张用 `vs` 拼接）直接交给 provider。
规格 §7 要求的是 **2-3 个具体、可回答的研究问题**，并且必须能**拒绝**那些答案
在公司内部的问题。两者的差别是结构性的：

- policy 回答「是不是事实争议」，research 回答「问什么、能不能问」。
- 问题以疑问句形式、逐条绑定到窗口内的一条主张（`supports_query_id` =
  `claim_N`），并通过 `ExternalEvidence.answers_question` 一路带到 prompt 与
  报告，读者因此能看出「这条网络资料回答的是哪条会议主张」。
- **六类禁止项**（内部排期 / 内部负责人能力 / 商务合同 / 私有业务数据 /
  内部成本 / 未公开架构事实）在 `FORBIDDEN_CATEGORIES` 中逐一列出。
  拒绝是**逐问题**而非逐窗口：同一窗口里既有商务支线又有技术主张时，
  技术问题不会被商务问题连坐。
- 被拒研究在 `SearchMetrics.internal_information_required` 与
  `research_blocked_categories` 中**打标**（§7 的原文要求），但**不写进
  `uncertainty`**：策略拒绝搜索在会议场景里是常态，每份报告都写一句会训练
  读者跳过 `uncertainty`。标记进日志，`uncertainty` 留给读者真正需要权衡的缺口。
- `source_quality`（official / standard / reputable / secondary）由
  `quality_for(url)` **按域名**赋类，是**启发式**，已在此如实声明——系统无法
  核实网页内容，能做的是诚实标出来源层级，而不是给它打分。
- `enrich` 的「永不抛异常」契约现在在**调用方**也加了兜底（`_safe_enrich`）：
  规格的「检索失败不得让队长失败」是无条件要求，不能只建立在被调用方的自觉上。

Prompt 侧同步补了信息层级规则 P1-P4（会议事实 > 外部资料 > 模型推断、
内部信息不可外部解决、禁止把网络内容伪装成会议事实）。这些规则**随
`<external_evidence>` 块一起下发**，因此单通道 `build_report_prompt` 与
两阶段 Deep 共享同一套排序，不存在「同一个文档因开关不同而行为不同」。

### 已知限制 / 技术债

1. **Brave 解析未经真实响应验证**（见 Status）。防御式解析 + 缺字段安全降级，
   但**契约是假设**。Phase 3-2E 第一件事是抓真实脱敏响应并对比。
2. **策略是规则，不是模型**。误判存在：内部话题里混入强事实标记时可能漏搜或
   多搜。这是 3-2E 需要观测的指标之一（`search_policy_type` 分布）。
3. **缓存是进程内的**，多实例部署时各实例各有一份。MVP 单进程，符合现状。
4. **`BraveSearchProvider` 无重试**（`max_retries=0` 且刻意无对应配置）。
   900ms 预算下重试基本等于「更慢地返回空」。
5. **未做**：DeepResearch、整页抓取、多轮 ReAct、reranker、模型侧
   `external_evidence` 生成——均按 §13/§6 明确排除在第一版之外。
6. **`source_quality` 只看域名**，不看内容、不看发布时间、不做交叉验证。
   它是给读者的线索，不是可信度结论；同一域名下的官方文档与营销页同分。
7. **禁止项匹配是子串匹配**，两个方向都可能误判：内部术语的贴边表述可能漏拒，
   而技术问题里夹带「成本」等词可能被误拒。误拒的代价是一次未执行的搜索
   （可见于 `research_blocked_categories`），误放的代价是伪造的权威——两者
   不对称，故刻意偏向拒绝。

### 未开始

Phase 3-2E：Brave vs Exa A/B 实测评估。本阶段完成后**不继续优化**。
