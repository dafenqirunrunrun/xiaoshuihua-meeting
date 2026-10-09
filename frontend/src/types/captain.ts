/**
 * 水华队长（Captain）报告类型 —— 严格映射后端 `CaptainReportPublic`
 * （`backend/app/captain/schemas.py`，ADR-002）。
 *
 * 与 Meeting State 的关系：**两个独立数据域**，唯一的共同点是都读取同一份
 * 只读的 FINAL transcript 事实源。Captain 不派生自 Meeting State，反之亦然。
 *
 * 三条硬约束（ADR-002 明确禁止事项，前端同样受约束）：
 *  1. **没有 side_a / side_b**。`positions` 是模型从转写中自主识别的 1..5 个
 *     观点簇，不是预设的两方。UI 禁止把它渲染成「正方 / 反方」。
 *  2. **禁止呈现 speaker 身份**。`Position` 只有 `label` / `statement` /
 *     `source_segment_seqs`，没有人物字段 —— 本系统的话者身份不可靠，
 *     裁判报告承载人名等于制造错误归因。
 *  3. `dispute_points` **允许为空**，且这是常见且正常的结果。「不强行制造
 *     冲突」是结构性保证；UI 在空分歧时显示「未发现分歧」，绝不暗示缺内容。
 */

/** 一个观点簇：中性的短标签 + 其主张。刻意不是一个人。 */
export interface CaptainPosition {
  label: string
  statement: string
  /** 支撑该观点的已持久化 FINAL `segment_seq`（证据溯源） */
  source_segment_seqs: number[]
}

/** 一条共识或一条分歧，带其依据。 */
export interface CaptainPointItem {
  text: string
  source_segment_seqs: number[]
}

/** 一条逐字引文及其所支撑的主张。 */
export interface CaptainEvidenceItem {
  segment_seq: number
  quote: string
  supports: string
}

/**
 * 一条外部资料（Phase 3-2）。**不是**转写片段：它没有 `segment_seq`，
 * 因此永远不能参与「查看原文」定位。
 *
 * `source_quality` 与 `answers_question` 由**服务端流水线**按域名 / 研究问题
 * 赋值，不是 provider 返回、也不是模型生成的。UI 只能如实展示，不得自行推断
 * 「这条更权威」—— 系统无法核实网页内容，能做的是诚实标出来源层级。
 */
export interface CaptainExternalEvidence {
  source_id: string
  title: string
  url: string
  context: string
  domain: string
  retrieved_at: string
  provider: string
  /** official / standard / reputable / secondary（启发式，见上） */
  source_quality: string
  /** 该资料回答的研究问题句柄（如 claim_1） */
  answers_question: string
  /**
   * --- Phase C.5 增量字段 ---
   *
   * 全部可选：Phase C.5 之前落盘的 `report_json` 没有这些键，把它们标成
   * 必填会让老报告在类型层「看起来完整、运行时是 undefined」。后端对老行
   * 的行为是如实缺省，前端也必须能如实渲染缺失。
   */
  /**
   * 这是**什么类型**的资料（official / documentation / standard / paper /
   * media / community / other）。与 `source_quality`（**多可信**）是两个独立
   * 轴：github.com 是 community 类型但可能 reputable。空串 = URL 无法归类
   * （**不是** other，other 表示「看了但都不匹配」）。
   */
  source_type?: string
  /** support / contradict / contextual —— 绝大多数是 contextual，见后端注释 */
  relation?: string
  /** 产生这条结果的**实际查询串** */
  query?: string
  /** 检索结果级摘要（**不是**网页正文 —— 系统从未抓取过页面） */
  evidence_summary?: string
  /** provider 报告的时间；**绝不**由模型或域名推断，缺失就是空 */
  published_at?: string
}

/**
 * 对一个主张的证据强度评估。**没有百分比**。
 *
 * 刻意不是 `0.8` 这类数字：模型无法为一个凭什么这么说的数字负责。等级
 * （低 / 中 / 中高 / 高）+ 依据才是可核对的说法 —— 读者能反驳依据，
 * 不能反驳「0.8」。
 */
export interface CaptainEvidenceStrength {
  claim: string
  strength: string
  basis: string
  source_segment_seqs: number[]
}

/**
 * 假设 —— 模型为了继续推理而**需要成立**、但会议未确认的前提。
 *
 * 与 `missing_evidence` 分开：`assumptions` 是被用到的前提，
 * `missing_evidence` 是缺的材料。合成一个「notes」字段会在模型赶时间时
 * 立刻塌成一种。
 */
export interface CaptainAssuranceGap {
  text: string
  source_segment_seqs: number[]
}

/** Quick 阶段：1-2 秒的第一段结果，字段数由 schema 钉死，写不长。 */
export interface CaptainQuickAssessment {
  issue: string
  /** 1..3 条最关键的转写引文 */
  key_evidence: CaptainEvidenceItem[]
  provisional_judgement: string
  question_to_resolve: string
}

/**
 * Deep 阶段：完整裁判推理。
 *
 * `supersedes` 非空 = 本次**修正了** Quick 的临时判断。这是允许的，但
 * 契约要求：非空 `supersedes` 必须伴随 `judgement` 里的重述 —— **不允许
 * 静默反转**。UI 在 `supersedes` 非空时单独渲染一条「已修正」提示，
 * 而不是让读者从两段文本里自己发现矛盾。
 */
export interface CaptainDeepAssessment {
  issue: string
  /** 1..5 个主张簇；`positions` 由它派生 */
  claims: CaptainPosition[]
  evidence: CaptainEvidenceItem[]
  counter_evidence: CaptainEvidenceItem[]
  /** 会议已确认的事实 */
  confirmed_facts: CaptainPointItem[]
  assumptions: CaptainAssuranceGap[]
  missing_evidence: CaptainPointItem[]
  evidence_strength: CaptainEvidenceStrength[]
  judgement: string
  recommendation: string
  citations: CaptainEvidenceItem[]
  /** 非空即表示修正了 Quick 的临时判断（必须非静默） */
  supersedes: string
}

/** 报告正文 —— 与后端 `CaptainReportContent` 一一对应。 */
export interface CaptainReportContent {
  discussion_summary: string
  /** 1..5。数量由模型决定，UI 不做「应该有 N 个」的假设。 */
  positions: CaptainPosition[]
  agreement_points: CaptainPointItem[]
  /** 允许为空 —— 空 = 未发现分歧，不是「数据缺失」。 */
  dispute_points: CaptainPointItem[]
  evidence: CaptainEvidenceItem[]
  /** 必填且非空：一份说不出自己不知道什么的报告，不是裁判的报告。 */
  uncertainty: string
  suggestion: string
  /** 服务端流水线写入的外部资料；空是常态（未搜索 / 未返回）。 */
  external_evidence: CaptainExternalEvidence[]
  /**
   * 外部资料检索这次**实际怎么结束的**（Phase C.5）。**是字符串不是布尔**：
   * `partial`（检索到了一些但没做完）与 `failed`（压根没检索成）对读者是
   * 两件不同的事，合并成一个 bool 就再也区分不开。
   *
   * `not_required` 是默认值 —— Phase C.5 之前的报告、以及本窗口无需外部事实
   * 的报告，都是这个值。取值见后端 `RESEARCH_STATUSES`。
   */
  research_status?: string
  /**
   * 两阶段裁判的两个阶段结果。**两个都为 null 是合法的** —— 单通道路径
   * 产出的报告永远如此，且老报告也是这个形状。UI 必须在两阶段字段缺失时
   * 回退到上面的扁平字段，而不是渲染一片空白。
   */
  quick: CaptainQuickAssessment | null
  deep: CaptainDeepAssessment | null
}

/**
 * 报告生命周期。与 Meeting 四态机**无关**，是 Captain 自己的状态。
 *
 * PENDING → ANALYZING → COMPLETED（终态，永不覆盖）
 *                    ↘ FAILED   （终态，永不覆盖）
 */
export type CaptainReportStatus = 'PENDING' | 'ANALYZING' | 'COMPLETED' | 'FAILED'

/** `GET /api/meetings/:id/captain/latest` 与 `/{report_id}` 的响应投影。 */
export interface CaptainReportPublic {
  report_id: string
  meeting_id: string
  status: CaptainReportStatus
  /** 触发本次裁判的 FINAL `segment_seq`（唤醒词所在句或当前高水位） */
  anchor_segment_seq: number
  window_from_seq: number
  window_to_seq: number
  created_at: string | null
  updated_at: string | null
  /**
   * 正文。仅 `COMPLETED` 时非 null —— 失败**绝不留下半份报告**
   * （`report_json` 保持 NULL），所以 UI 要么渲染完整报告，要么如实说明失败。
   */
  report: CaptainReportContent | null
}

/**
 * `POST /api/meetings/:id/captain` 的 202 响应：一份正在飞行的报告的持久句柄。
 *
 * 202 而不是同步返回报告：一次裁判 = 一次完整 LLM 调用，同步会长时间占用
 * 单进程 HTTP worker，且刷新即丢结果。客户端拿 `report_id` 后靠 SSE 收
 * `captain.completed`，断线则 `GET /captain/{report_id}` 恢复（SSE 不可回放）。
 */
export interface CaptainTriggerAccepted {
  report_id: string
  meeting_id: string
  status: CaptainReportStatus
  anchor_segment_seq: number
  /** 本次是否真的新建了报告（false = 该 anchor 已分析过，零 LLM 花费） */
  created: boolean
  /** created 的反面；语义化命名便于调用方按意图读取 */
  reused: boolean
}
