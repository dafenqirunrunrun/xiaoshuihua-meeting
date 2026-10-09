import { useCallback, useEffect, useRef, useState } from 'react'
import {
  getCaptainReport,
  getLatestCaptainReport,
  normalizeApiError,
  triggerCaptain,
} from '@/api'
import type { CaptainQuickEventPayload, CaptainStreamEvent } from '@/hooks/useMeetingStream'
import {
  applyLoadedRun,
  claimRun,
  createCaptainRunOwnership,
  decideQuick,
  decideTerminal,
  resetRun,
  type CaptainRunOwnership,
} from '@/hooks/captainRunOwnership'
import type { CaptainReportPublic } from '@/types/captain'

export interface CaptainRefereeResult {
  /** 最近一次裁判报告（含 PENDING / ANALYZING / FAILED 的诚实状态）；未取到前为 null */
  report: CaptainReportPublic | null
  /**
   * 两阶段裁判第一段（Quick）的临时结果，**仅当它属于当前这份报告、
   * 且该报告尚未落盘为 COMPLETED 时**非 null。
   *
   * 三条绑定规则，缺一不可：
   *  1. `report_id` 必须等于当前展示报告的 id —— 否则上一份报告的临时结果
   *     会挂在这一次上（用户在等待时会看到一段与本次无关的判断）。
   *  2. 报告一旦 COMPLETED / FAILED，`quick` 立即归位 null：durable 报告是
   *     唯一事实源，此时 `report.quick` / `report.deep` 才是权威版本，继续展示
   *     SSE 快照会与落盘版本产生第二种说法。
   *  3. 换会议 / 重新触发时清空（见下方 effect）。
   *
   * 值为 null 不代表出错：绝大多数情况下报告会直接在 ANALYZING 结束后带上
   * 完整内容，临时卡片只是一个更早的中间态。
   */
  quick: CaptainQuickEventPayload | null
  /** 正在读取报告（GET） */
  loading: boolean
  /** 正在请求一次新的裁判（POST 202 往返中） */
  triggering: boolean
  /**
   * 错误归一化（喂给展示层，不暴露技术细节）：
   *  - ''            无错误
   *  - 'no_report'   该会议从未被裁判过（后端 404）—— **正常状态**，不是故障
   *  - 'not_found'   会议不存在
   *  - 'throttled'   距上次裁判不足节流间隔（后端 429）
   *  - 'network'     网络 / 5xx
   *  - 其它          具体错误消息
   */
  error: string
  /** 请求一次裁判（按钮 / 快捷发送共用）。不传 anchor —— 由服务端锚定。 */
  trigger: () => Promise<void>
  /**
   * 裁判 SSE 事件的**唯一**入口（terminal 与 quick 都在这里收口）。
   *
   * 两种事件的处理方式相反 —— terminal 回源 GET、quick 直接渲染 —— 但归属
   * 判定必须由**这一个** owner 做。拆成两个回调时，接受另一份报告的 quick 时
   * 就没有任何东西知道「当前报告已经落盘终态」，那张临时卡片会挂到已完成报告
   * 上（这正是 Phase C.5 §5 要修的错归因）。断言见
   * `captainRunOwnership.test.ts`。
   */
  handleCaptainEvent: (event: CaptainStreamEvent) => void
  /**
   * 手动回源（刷新按钮 / 重连恢复）。传 `reportId` 精确刷新那一份，省略则读
   * latest。**注意**：这不是 SSE 回调，SSE 走 `handleCaptainEvent`。
   */
  invalidate: (reportId?: string | null) => void
}

/**
 * 水华队长裁判数据钩子（ADR-002 Phase 2-A/C 前端接入）。
 *
 * 数据模型与 `useMeetingIntelligence` 一致的纪律：
 *  - `GET /captain/{id|latest}` 是**唯一事实源**；
 *  - `captain.completed` / `captain.failed` 只是**失效通知**。即使它例外地
 *    携带了 `positions` / `dispute_points` / `uncertainty`（后端为即时渲染做的
 *    刻意偏离），本 hook 依然回源 GET —— 因为 payload 是完整报告的一个**子集**
 *    （没有 `discussion_summary` / `agreement_points` / `evidence` / `suggestion`），
 *    用它渲染会得到一份看起来完整、实则残缺的报告。这比慢一点更糟。
 *  - 失败**不清空** last-good：用户已经看到的裁判结论不该因为一次刷新失败消失。
 *
 * 状态留在 hook 内，**不进全局 store** —— Captain 与 Transcript 是两个独立数据域。
 *
 * 三道守卫：
 *  1. request sequence（乱序丢弃）
 *  2. meetingId 绑定（防跨会议串数据）
 *  3. **run 身份**：当前在等的是哪一份报告，terminal 与 quick 事件的归属
 *     都以它为准 —— 由 `runRef` 持有，规则在 `captainRunOwnership.ts`。
 *
 * 关于第 3 条（Phase C.5 §5 修的缺陷）：没有版本号可用 —— `captain_report` 是
 * append-only 的追加式记录，每份报告有唯一 `report_id`。所以 `report_id` 就是
 * 这里的版本。关键是**认领身份的时机**：一次触发之后、GET 回来之前，若身份
 * 还停在上一份，本报告自己的 quick 事件会被误判为「别人的」而丢弃。所以
 * `trigger` / 显式 `invalidate` 都在发请求**之前**就 `claimRun`，而不是等 GET
 * 落地 —— 这正是把两阶段设计维持成两阶段的那个细节。
 */
export function useCaptainReferee(
  meetingId: string | undefined,
  enabled: boolean,
): CaptainRefereeResult {
  const [report, setReport] = useState<CaptainReportPublic | null>(null)
  const [quick, setQuick] = useState<CaptainQuickEventPayload | null>(null)
  const [loading, setLoading] = useState(false)
  const [triggering, setTriggering] = useState(false)
  const [error, setError] = useState('')

  /** 请求序号：每次发起 +1，过期响应比对后丢弃 */
  const seqRef = useRef(0)
  /** meetingId 绑定：变化后作废所有在飞响应 */
  const meetingRef = useRef<string | undefined>(meetingId)
  /**
   * run 归属状态（当前展示的是哪一份 / 是否已终态）。
   *
   * 放在 ref 而不是 state，因为 SSE 回调可能在**同一批 state 更新之间**到达，
   * 读 state 会拿到上一个 render 的陈旧值；归属判定必须读「最后一次写入」。
   *
   * 规则本身（哪些事件属于当前 run）**不在这里**，而是纯函数，见
   * `captainRunOwnership.ts` —— 这个文件负责时机，那个文件负责判定，判定因此
   * 可以被直接测试（本项目没有组件测试运行器）。
   */
  const runRef = useRef<CaptainRunOwnership>(createCaptainRunOwnership())

  const applyOne = useCallback(
    (mid: string, incoming: CaptainReportPublic, seq: number) => {
      if (seq !== seqRef.current || mid !== meetingRef.current) return
      const { clearQuick } = applyLoadedRun(
        runRef.current,
        incoming.report_id,
        incoming.status,
      )
      setReport(incoming)
      // 落盘终态 → 临时结果退场（绑定规则 2）。COMPLETED 时 durable 报告里
      // 的 quick/deep 才是权威；FAILED 时留着这段临时判断会暗示「分析其实
      // 成功了」，是对失败的不诚实呈现。
      if (clearQuick) setQuick(null)
    },
    [],
  )

  const load = useCallback(
    (mid: string, reportId?: string) => {
      seqRef.current += 1
      const seq = seqRef.current
      setLoading(true)
      setError('')

      const request = reportId
        ? getCaptainReport(mid, reportId)
        : getLatestCaptainReport(mid)

      request
        .then((data) => applyOne(mid, data, seq))
        .catch((raw: unknown) => {
          if (seq !== seqRef.current || mid !== meetingRef.current) return
          const normalized = normalizeApiError(raw)
          if (normalized.kind === 'http' && normalized.status === 404) {
            // 404 分两种，但产品表现一致：「这个会议还没有可展示的裁判报告」。
            //  - latest：会议从未被裁判过（最常见，完全正常）
            //  - 按 id：该报告不存在（理论上不该发生，可能是后端数据被清理）
            // 两者都不是故障，所以不报错、不重试，如实显示「还没有」。
            setError('no_report')
          } else if (
            normalized.kind === 'network' ||
            (normalized.kind === 'http' && (normalized.status ?? 0) >= 500)
          ) {
            setError('network')
          } else {
            setError(normalized.message || '裁判报告加载失败')
          }
        })
        .finally(() => {
          if (seq === seqRef.current && mid === meetingRef.current) setLoading(false)
        })
    },
    [applyOne],
  )

  // meetingId / enabled 变化 → 整体归零（不跨会议串数据）
  useEffect(() => {
    meetingRef.current = meetingId
    seqRef.current += 1
    resetRun(runRef.current)
    setReport(null)
    setQuick(null)
    setError('')
    setLoading(false)
    setTriggering(false)

    if (!meetingId || !enabled) return
    load(meetingId)
  }, [meetingId, enabled, load])

  /**
   * 裁判 SSE 事件的**唯一**入口 —— terminal 与 quick 的归属判定都必须在这里。
   *
   * 两种事件的处理方式相反（terminal 回源 GET、quick 直接渲染），但归属规则
   * 共用同一份状态，所以不能拆成两个回调：拆开后「接受 B 的临时结果」时没有
   * 任何东西知道「A 已经落盘终态」。判定本身是纯函数（`captainRunOwnership`），
   * 这里只负责时机与副作用。
   */
  const handleCaptainEvent = useCallback(
    (event: CaptainStreamEvent) => {
      const mid = meetingRef.current
      if (!mid) return

      if (event.kind === 'terminal') {
        const decision = decideTerminal(runRef.current, event.reportId)
        // 迟到的**另一份**报告的终态：与当前展示无关。不 load、不动 seq —— 既
        // 不该覆盖用户正在看的新报告，也不该作废新报告正在飞的 GET。
        if (decision.action === 'ignore') return
        setQuick(null)
        load(mid, decision.reportId ?? undefined)
        return
      }

      const p = event.quick
      if (!p) return
      if (!decideQuick(runRef.current, p.report_id)) return
      setQuick(p)
    },
    [load],
  )

  const trigger = useCallback(async () => {
    const mid = meetingRef.current
    if (!mid) return
    setTriggering(true)
    setError('')
    try {
      const accepted = await triggerCaptain(mid)
      if (mid !== meetingRef.current) return
      // 立即认领这一份：POST 202 已经确定了 report_id，GET 只是读内容。不认领
      // 的话，GET 往返期间该报告自己的 quick 事件会因为身份还停在旧值被丢弃
      // —— 而 quick 的全部价值就是不等 Deep，丢它就等于把两阶段退化成单阶段。
      // 越界防护：旧报告迟到的终态事件会被判定为过期运行（见 decideTerminal）。
      claimRun(runRef.current, accepted.report_id)
      setQuick(null)
      // 立刻回源读这一份：可能已是 COMPLETED（reused），也可能是 PENDING。
      // 无论哪种，UI 都从事实源渲染，不猜状态。
      load(mid, accepted.report_id)
    } catch (raw: unknown) {
      if (mid !== meetingRef.current) return
      const normalized = normalizeApiError(raw)
      if (normalized.kind === 'http' && normalized.status === 429) {
        setError('throttled')
      } else if (normalized.kind === 'http' && normalized.status === 503) {
        setError('busy')
      } else if (normalized.kind === 'http' && normalized.status === 404) {
        setError('not_found')
      } else if (
        normalized.kind === 'network' ||
        (normalized.kind === 'http' && (normalized.status ?? 0) >= 500)
      ) {
        setError('network')
      } else {
        setError(normalized.message || '请求水华队长失败')
      }
    } finally {
      if (mid === meetingRef.current) setTriggering(false)
    }
  }, [load])

  /**
   * 手动回源（刷新按钮 / 重连恢复）。
   *
   * 与 `handleCaptainEvent` 的分工：这是**用户/恢复驱动**的读取，不经 SSE
   * 归属判定。传 `reportId` 时立即认领身份 —— 否则 GET 往返期间该报告自己的
   * quick 事件会因为 `reportIdRef` 还停在旧值被丢弃；同时也让「旧报告的迟到
   * 终态」在本函数认领新身份后被正确判定为过期运行。
   */
  const invalidate = useCallback(
    (reportId?: string | null) => {
      const mid = meetingRef.current
      if (!mid) return
      if (reportId) {
        claimRun(runRef.current, reportId)
        setQuick(null)
      }
      load(mid, reportId ?? undefined)
    },
    [load],
  )

  return { report, quick, loading, triggering, error, trigger, invalidate, handleCaptainEvent }
}
