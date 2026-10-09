import { useEffect, useRef, useState } from 'react'
import type { CaptainReportPublic } from '@/types/captain'

/**
 * 水华队长的**表现状态**（Phase 3-1）。
 *
 * 这是一个纯派生视图，不是第二个数据源。它没有自己的求值逻辑：所有分支都读
 * `useCaptainReferee` 交出来的真实字段（`report.status` / `loading` /
 * `triggering` / `error`）。因此 SSE 断了、GET 失败了、报告是 PENDING 还是
 * ANALYZING —— 角色照着说，不多说一个字。
 *
 * 三条纪律（Phase 3-1 明确约束）
 * -----------------------------
 *  1. **绝不 setTimeout 假装分析完成**。COMPLETED 只在 `report.status ===
 *     'COMPLETED'` 时出现 —— 也就是服务端真的落库了一份完整报告之后。
 *  2. **绝不由 payload 推导**。状态只从 `CaptainReportStatus` 来，不从转写、
 *     不从 SSE payload 的报告片段来（那是子集，见 useCaptainReferee 文档）。
 *  3. **不新增状态**。前端表现层不发明后端没有的阶段；`TRIGGERED` / `FAILED`
 *     是同一批真实字段的不同解读，不是新事实。
 *
 * 状态来源对照
 * ------------
 * | 表现状态   | 真实来源                                                        |
 * |-----------|----------------------------------------------------------------|
 * | IDLE      | 无报告、无请求、无错误（会议从未被裁判，或还没触发）                |
 * | TRIGGERED | `triggering`（POST /captain 的 202 往返中）                      |
 * | ANALYZING | `report.status ∈ {PENDING, ANALYZING}`，或首次读数尚无报告         |
 * | COMPLETED | `report.status === 'COMPLETED'` 且正文非空                       |
 * | FAILED    | `report.status === 'FAILED'`，或读不到报告（no_report 除外）       |
 *
 * `no_report` 刻意**不**算失败：那是「这个会议还没有裁判记录」这一正常状态
 * （后端 404），把它渲染成失败会凭空制造一次故障。
 */
export type PixelCaptainState =
  | 'IDLE'
  | 'TRIGGERED'
  | 'ANALYZING'
  | 'COMPLETED'
  | 'FAILED'

export interface PixelCaptainView {
  phase: PixelCaptainState
  /** 角色旁的一句话，与 phase 一一对应。纯 UI 文案，不承载数据。 */
  caption: string
  /** 有报告正文可展开（COMPLETED 且 report_json 非空） */
  hasReport: boolean
  /** 报告正文是否已展开 */
  expanded: boolean
  toggle: () => void
}

/** 各阶段的角色台词。刻意与 CaptainPanel 的标题错开，避免同一句话出现两次。 */
const CAPTION: Record<PixelCaptainState, string> = {
  IDLE: '等待召唤',
  TRIGGERED: '水华队长已收到',
  ANALYZING: '正在回顾刚才的讨论…',
  COMPLETED: '分析完成',
  FAILED: '本次分析未完成',
}

interface CaptainViewInput {
  report: CaptainReportPublic | null
  loading: boolean
  triggering: boolean
  error: string
}

/**
 * 从 captain hook 的真实字段派生表现状态。
 *
 * 判断顺序即优先级：正在触发 > 读到了终态 > 还在跑 > 空闲。把 `triggering`
 * 放在最前，是因为用户刚点下按钮时最需要看到「已收到」的回执，哪怕上一份
 * 报告还是 COMPLETED。
 */
export function derivePixelCaptainPhase({
  report,
  loading,
  triggering,
  error,
}: CaptainViewInput): PixelCaptainState {
  if (triggering) return 'TRIGGERED'

  if (report) {
    if (report.status === 'COMPLETED') {
      // 正文为空的 COMPLETED 在后端不该出现（失败不留半份报告）；真出现时
      // 按 FAILED 处理 —— 宁可说「没完成」，也不显示一个空报告。
      return report.report ? 'COMPLETED' : 'FAILED'
    }
    if (report.status === 'FAILED') return 'FAILED'
    // PENDING / ANALYZING：服务端已受理，分析在进行中
    return 'ANALYZING'
  }

  if (loading) return 'ANALYZING' // 首次读数尚未回来，等同「正在回顾」
  // no_report 是正常状态（从未被裁判过），不是失败
  if (error && error !== 'no_report') return 'FAILED'
  return 'IDLE'
}

/**
 * @param meetingIsLive 会议是否处于可裁判状态（IN_PROGRESS / FINALIZING）。
 *   会议结束后角色收起为纯空闲态 —— 它不该在归档态里假装随时待命。
 */
export function usePixelCaptainState(
  input: CaptainViewInput,
  meetingIsLive: boolean,
): PixelCaptainView {
  const { report, loading, triggering, error } = input
  const phase = derivePixelCaptainPhase({ report, loading, triggering, error })
  const hasReport = phase === 'COMPLETED'

  const [expanded, setExpanded] = useState(false)

  // 报告身份变化 → 重新收起。新的一份报告不该继承上一份的展开状态；
  // 这也让「做一次 Judge 动作，然后展示 CaptainPanel」(§一) 自然成立。
  const reportIdRef = useRef<string | null>(null)
  const reportId = report?.report_id ?? null
  useEffect(() => {
    if (reportId !== reportIdRef.current) {
      reportIdRef.current = reportId
      setExpanded(false)
    }
  }, [reportId])

  return {
    phase: meetingIsLive ? phase : 'IDLE',
    caption: CAPTION[meetingIsLive ? phase : 'IDLE'],
    hasReport,
    // 没有正文时 `expanded` 无意义，一律报 false，避免调用方拿到悬空状态
    expanded: hasReport && expanded,
    toggle: () => setExpanded((prev) => !prev),
  }
}
