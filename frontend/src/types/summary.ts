/** Summary 状态：与后端 SummaryOut.status 一致（PRD §12） */
export type SummaryStatus =
  | 'NOT_STARTED'
  | 'GENERATING'
  | 'COMPLETED'
  | 'FAILED'

/**
 * GET /api/meetings/:id/summary 返回（SummaryOut，Step 7B §15）。
 * Durable 恢复事实源：SSE summary.* 事件非 durable、不回放，刷新后靠本接口恢复。
 */
export interface MeetingSummary {
  id: number
  meeting_id: string
  version: number
  status: SummaryStatus
  overview: string | null
  key_topics: string[]
  conclusions: string[]
  open_questions: string[]
  model_name: string | null
  raw_model_output: string | null
  /**
   * UI-INTEL-2B：可选的结构化 evidence projection（后端 public read path）。
   *
   * `null` = 该 row 不是可验证的 V2 FinalMeetingSummary（legacy / fallback /
   * FAILED / 空会议 / 解析失败）→ 不渲染任何「查看原文」入口。
   *
   * **前端绝不从 `raw_model_output` 解析 evidence**（§4）—— 那是内部字段，
   * 形状随生成路径变化（V2 JSON / legacy JSON / `"[summary failed] …"`）。
   * 唯一的 evidence 来源就是这个正式 projection。
   */
  summary_evidence: SummaryEvidence | null
  created_at: string | null
}

/**
 * 一条 decision + 它引用的 transcript seqs（后端 SummaryEvidenceDecision）。
 *
 * seqs 是**模型自报**的，后端原样透传、不做归一化 —— 归一化（合法整数 /
 * 去重 / 升序 / 上限）统一在 `utils/evidence.ts`，只有一份实现。
 * 引用不存在的 seq 是**可到达的正常状态**（AGENT-1 Known Limitation #4），
 * 不是错误：UI 按 §8 优雅降级。
 */
export interface SummaryEvidenceDecision {
  text: string
  source_segment_seqs: number[]
}

/** 一个核心议题 + 摘要 + 原文依据 */
export interface SummaryEvidenceTopic {
  title: string
  summary: string
  source_segment_seqs: number[]
}

/** 一条 action item + 它引用的 seqs（owner / deadline 可为 null → UI 显示「待确认」） */
export interface SummaryEvidenceActionItem {
  task: string
  owner: string | null
  deadline: string | null
  source_segment_seqs: number[]
}

/** 一条 risk + 它引用的 seqs */
export interface SummaryEvidenceRisk {
  text: string
  source_segment_seqs: number[]
}

/** 一条 open question + 它引用的 seqs */
export interface SummaryEvidenceOpenQuestion {
  text: string
  source_segment_seqs: number[]
}

/**
 * V2 Final Summary 的结构化 evidence（后端 SummaryEvidence）。
 *
 * 五个列表都可能是空的：overview / closing_summary 是散文、不引用任何句子，
 * 而一次真的没有决议的会议本来就会产生空列表。空 ≠ 解析失败。
 */
export interface SummaryEvidence {
  topics: SummaryEvidenceTopic[]
  decisions: SummaryEvidenceDecision[]
  action_items: SummaryEvidenceActionItem[]
  risks: SummaryEvidenceRisk[]
  open_questions: SummaryEvidenceOpenQuestion[]
}

/** POST /api/meetings/:id/summary/regenerate 的 202 响应（RegenerationAccepted） */
export interface RegenerationAccepted {
  meeting_id: string
  version: number
  status: 'GENERATING'
}

/** Pydantic 校验通过的结构化Summary（渲染层直接消费 overview/topics/conclusions/questions） */
export interface MeetingSummaryContent {
  overview: string
  key_topics: string[]
  conclusions: string[]
  open_questions: string[]
}
