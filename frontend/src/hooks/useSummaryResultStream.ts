import { useEffect, useRef } from 'react'
import { getSummary } from '@/api'
import { subscribeSseEnvelopes } from '@/utils/sse'

/** 退避：1s → 2s → 4s → 8s → max 10s（与 useMeetingStream 对齐） */
const BASE_DELAY = 1000
const MAX_DELAY = 10000

function backoffDelay(attempt: number): number {
  return Math.min(BASE_DELAY * 2 ** attempt, MAX_DELAY)
}

export interface UseSummaryResultStreamParams {
  meetingId: string | undefined
  /**
   * 当前 Summary 是否 terminal（COMPLETED / FAILED）。
   * true → 不连接 / 关闭 SSE（terminal 后无需通知，手动刷新兜底）。
   */
  terminal: boolean
  /**
   * summary.started / completed / failed / meeting.status 到达 → 回调。
   * 调用方收到后再 GET /summary（或 GET /meeting）—— SSE 只是通知通道，
   * GET 才是 durable 事实源。
   */
  onSummaryEvent: () => void
}

/**
 * Result 页专属 Summary 生命周期 SSE（Step 8C-2）。
 *
 * 角色：**仅 Summary 的实时通知通道**，绝不承担数据事实源：
 *  - 连接条件：仅当 summary 非 terminal（meeting FINALIZING / summary
 *    missing / GENERATING）时建立 EventSource；
 *  - summary.started → 通知（UI 转「正在生成」，不造假进度）；
 *  - summary.completed / summary.failed → 通知 → 调用方 **GET /summary**
 *    渲染持久化快照（SSE payload 只有 {version,status}，永不直接当 Summary 用）；
 *  - meeting.status(COMPLETED) → 调用方 GET /meeting（Server 是状态事实源）；
 *  - terminal 后关闭 EventSource；「重新生成中」再连（下次 render terminal=false）。
 *
 * 断线恢复（onerror）：主动 close → 指数退避 → 重新 GET /summary（GET 是恢复源，
 * 与 useMeetingStream 的 Snapshot-recovery 同构）→ 若已 terminal 则不重连，否则重建 SSE。
 *
 * 硬约束：
 *  - 后端发的是**具名帧**（`event: summary.completed`），不会触发 onmessage ——
 *    必须逐事件名订阅（见 utils/sse.ts 的 subscribeSseEnvelopes）；
 *  - 不消费 transcript.partial / transcript.final —— Result 是归档阅读态
 *    （Transcript 已由 useTranscriptSnapshotResult 从 GET 恢复）；
 *  - 不调用 regenerateSummary —— 本钩子只 `listen`，不 `trigger`；
 *  - meetingId scope：任意时刻每个 meeting 最多一个 EventSource（StrictMode
 *    gen 计数防护过期回调）。
 */
export function useSummaryResultStream({
  meetingId,
  terminal,
  onSummaryEvent,
}: UseSummaryResultStreamParams) {
  const esRef = useRef<EventSource | null>(null)
  const timerRef = useRef<number | null>(null)
  const genRef = useRef(0)
  const retryRef = useRef(0)
  const terminalRef = useRef(terminal)
  const onEventRef = useRef(onSummaryEvent)
  terminalRef.current = terminal
  onEventRef.current = onSummaryEvent

  /** 关闭当前流 + 清退避 timer；gen++ 使过期回调静默失效。 */
  const closeStream = () => {
    genRef.current += 1
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current)
      timerRef.current = null
    }
    if (esRef.current) {
      esRef.current.close()
      esRef.current = null
    }
  }

  /** 断线恢复：GET /summary 走到 terminal 则不重连，否则退避后重建 SSE。 */
  const handleRecovery = (mid: string) => {
    const gen = genRef.current
    const attempt = retryRef.current
    retryRef.current += 1
    const delay = backoffDelay(attempt)
    if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    timerRef.current = window.setTimeout(async () => {
      timerRef.current = null
      if (genRef.current !== gen) return // 已清理（离开页面 / 切换会议）
      if (terminalRef.current) return // 期间已到终态：不再需要实时通知
      try {
        // GET /summary 是 durable 恢复源 → 最新状态驱动重连决策
        const summary = await getSummary(mid)
        if (genRef.current !== gen) return
        onEventRef.current() // 调用方按持久化快照刷新 UI（含已 terminal 的情况）
        if (summary.status === 'COMPLETED' || summary.status === 'FAILED') {
          return // GET 已证明到达终态：不重建 SSE
        }
        openStream(mid)
      } catch {
        if (genRef.current !== gen) return
        handleRecovery(mid) // GET 失败（尚未终态）→ 继续退避重试
      }
    }, delay)
  }

  const openStream = (mid: string) => {
    if (esRef.current) return // 单连接防护：任意时刻最多一个 EventSource
    const gen = genRef.current

    const es = new EventSource(`/api/meetings/${mid}/events?after_segment_seq=0`)
    esRef.current = es

    es.onopen = () => {
      if (genRef.current !== gen) {
        es.close()
        return
      }
      retryRef.current = 0 // 成功连接后重置退避
    }

    es.onerror = () => {
      if (genRef.current !== gen) return
      if (esRef.current) {
        esRef.current.close()
        esRef.current = null
      }
      handleRecovery(mid)
    }

    // 后端发送**具名**帧（`event: summary.completed`），不会触发 onmessage；
    // 必须逐事件名订阅（见 utils/sse.ts）。
    const unsubscribe = subscribeSseEnvelopes(es, (env) => {
      if (genRef.current !== gen) return
      if (env.meeting_id !== mid) return // 防御：只处理本会议帧
      switch (env.event_type) {
        // 通知通道：只触发回调，绝不当事实源。刷新由调用方 GET /summary 完成。
        case 'summary.started':
        case 'summary.completed':
        case 'summary.failed':
          onEventRef.current()
          break
        // Server 是状态事实源：调用方 GET /meeting 覆盖 store，不猜 payload
        case 'meeting.status':
          onEventRef.current()
          break
        default:
          break // heartbeat / transcript.*（Result 是归档阅读态，不消费）
      }
    })

    // close/重连时移除订阅，避免悬挂监听
    const origClose = es.close.bind(es)
    es.close = () => {
      unsubscribe()
      origClose()
    }
  }

  // ---- 主生命周期：仅非 terminal 才建立 SSE；terminal 或离开 → 关闭 ----
  useEffect(() => {
    if (terminal || !meetingId) {
      closeStream()
      return
    }
    openStream(meetingId)
    return () => closeStream()
    // openStream / closeStream / handleRecovery 是本 hook 内部稳定的闭包，
    // meetingId / terminal 是控制连接的唯一输入（与 useMeetingStream 同模式）。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meetingId, terminal])

  // unmount 兜底：StrictMode cleanup 由上面 effect 负责；这里再保底一次
  useEffect(() => {
    return () => {
      genRef.current += 1
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
      if (esRef.current) {
        esRef.current.close()
        esRef.current = null
      }
    }
  }, [])
}
