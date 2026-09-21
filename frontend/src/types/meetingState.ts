/**
 * Meeting State（会议工作记忆）类型 —— 严格映射后端 `MeetingStatePublic`
 * （`backend/app/meeting_state/schemas.py`，Step UI-INTEL-1 的 public 投影）。
 *
 * 这是 AI 在会议进行中理解出的结构化内容，**不是**会后 Final Summary：
 *   Meeting State = 会中的工作记忆（随 FINAL 增量更新）
 *   Summary       = 会后一次性的派生终稿（见 types/summary.ts）
 * 两者是独立数据域，禁止互相推导。
 *
 * 前端只做展示：所有 decision / action item / risk 均来自后端 Meeting State，
 * 绝不在前端用关键词规则"猜"（那会把展示层的猜测伪装成 AI 结论）。
 */

/**
 * Meeting State 的工作状态（**不是** Meeting 四态机）。
 * Meeting 生命周期只有 CREATED / IN_PROGRESS / FINALIZING / COMPLETED；
 * 这里描述的是 AGENT-1 自身的处理状态，两个维度互不影响。
 */
export type MeetingStateStatus = 'EMPTY' | 'ACTIVE' | 'FAILED' | 'FROZEN'

/** 一条讨论议题 */
export interface TopicState {
  title: string
  summary: string
  /**
   * 该结论对应的已持久化 FINAL `segment_seq`（证据溯源）。
   * UI-INTEL-1 只接住数据、不实现点击跳转 —— 那属于 UI-INTEL-2。
   */
  source_segment_seqs: number[]
}

/** 一条已确认决策 */
export interface DecisionItem {
  text: string
  source_segment_seqs: number[]
}

/**
 * 一条待办事项。
 *
 * `owner` / `deadline` 在契约上**就是可为 null** —— 转写里经常没说负责人或
 * 时间，编造一个比说"不知道"更糟。UI 显示「待确认」纯属展示层 fallback，
 * **绝不写回后端**。
 */
export interface ActionItem {
  task: string
  owner: string | null
  deadline: string | null
  source_segment_seqs: number[]
}

/** 一个未决问题 */
export interface OpenQuestion {
  text: string
  source_segment_seqs: number[]
}

/** 一条风险 / 顾虑 */
export interface RiskItem {
  text: string
  source_segment_seqs: number[]
}

/**
 * GET /api/meetings/:id/state 响应（后端 `MeetingStatePublic`）。
 *
 * 不含 `model_name`（provider 身份是工程事实，不是产品事实），
 * 也不含 `last_error` / raw model output / prompt / 内部异常 —— 后端投影层
 * 结构性保证这些不会到达浏览器。
 */
export interface MeetingState {
  meeting_id: string
  /** 单调递增，只由后端在**成功**更新时推进（失败不推进）。 */
  version: number
  /** 幂等游标：已折入状态的 FINAL 最大 segment_seq。 */
  last_processed_segment_seq: number
  status: MeetingStateStatus
  updated_at: string | null
  current_topic: string | null
  topics: TopicState[]
  decisions: DecisionItem[]
  action_items: ActionItem[]
  open_questions: OpenQuestion[]
  risks: RiskItem[]
}
