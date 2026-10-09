import { useEffect, useRef } from 'react'
import { getMeeting, getTranscriptSnapshot } from '@/api'
import { useMeetingStore } from '@/stores/meetingStore'
import { subscribeSseEnvelopes } from '@/utils/sse'
import type { Meeting } from '@/types/meeting'
import type { PartialUtterance, TranscriptSegment } from '@/types/transcript'
import type { SseEnvelope } from '@/types/sse'

/**
 * 实时转写 SSE 流（Step 8B-4）。消费 Step 5 已实现的 gap-free 契约。
 *
 * 连接顺序（PRD §53/§54）：Snapshot 是持久化事实源，SSE 只是实时通知通道。
 *   GET Meeting → GET Transcript Snapshot → maxSegmentSeq → GET /events?after_segment_seq=maxSeq
 * 绝不先连 SSE 再等 Snapshot。
 *
 * 生命周期：
 *   - CREATED / COMPLETED：不连 / 关闭（COMPLETED 后无需实时字幕）。
 *   - IN_PROGRESS / FINALIZING：建立并保持 SSE。Finish → FINALIZING 不关流，
 *     ASR 可能仍发 trailing FINAL（Finish ≠ Transcript Stream End）。
 *   - unmount / meetingId 变化：close EventSource + 清 reconnect timer（gen 防护）。
 *
 * 断线恢复（onerror）：浏览器默认自动重连不可靠（后端恢复协议是
 * Snapshot + after_segment_seq），所以主动 close → clear PARTIAL（临时态不持久）
 * → 指数退避（1s/2s/4s/8s/max10s）→ 重新 GET Snapshot → setFinalSegments →
 * 重算 maxSeq → 以新游标重建 SSE。绝不依赖 Last-Event-ID。
 *
 * React StrictMode：cleanup close 使任意时刻每个 meeting 最多一个 active
 * EventSource（gen 计数防护过期回调 / 双连接）。
 *
 * meeting.status 事件：不猜 timestamps，调用 GET Meeting 用 server 响应覆盖
 * store（Server 是 Meeting Source of Truth）。
 *
 * SSE 永远不是事实源：断 ≠ 丢；刷新走 Snapshot 恢复。
 */
const BASE_DELAY = 1000
const MAX_DELAY = 10000

/**
 * `captain.quick_assessment` payload 的线上形状（见 docs/api-contract.md）。
 *
 * 唯一与 `CaptainQuickAssessment` 的差别是多了 `report_id` 与
 * `provisional` —— 前者让客户端知道这条临时结果属于哪一份报告，后者是
 * 后端的不变式（永远 true），UI 用它做防御而不是做判断。
 */
export interface CaptainQuickEventPayload {
  report_id: string
  provisional: true
  issue: string
  key_evidence: { segment_seq: number; quote: string; supports: string }[]
  provisional_judgement: string
  question_to_resolve: string
}

function backoffDelay(attempt: number): number {
  return Math.min(BASE_DELAY * 2 ** attempt, MAX_DELAY)
}

export interface UseMeetingStreamParams {
  meetingId: string | undefined
  status: Meeting['status'] | undefined
  /** Snapshot 首次加载成功后才为 true —— 是建立 SSE 的前置条件 */
  snapshotReady: boolean
  /** 已持久化 FINAL 的最大 segment_seq（作为连接游标 after_segment_seq） */
  maxSegmentSeq: number
  /**
   * `meeting.state.updated` 到达时的失效回调（Step UI-INTEL-1）。
   *
   * **复用本 hook 已有的 `/events` EventSource**，不新增第二条 SSE 连接：
   * Meeting State 的失效通知与 Transcript 增量走的是同一个流。
   *
   * 语义纯为 invalidation —— 事件只带 `{version, status}`，调用方应重新
   * `GET /state`，绝不把 payload 当内容渲染。此处不对回调做任何 await / 错误
   * 传播：Meeting State 拉取失败绝不影响转写链路。
   *
   * 存在 `Ref` 里（见下方 `onStateInvalidatedRef`），故意**不**进 effect 依赖 ——
   * 否则回调每次 render 换新引用都会重连 SSE。
   */
  onStateInvalidated?: () => void
  /**
   * 裁判事件回调（ADR-002 Phase 2-A + Task 6），**一个入口覆盖三种事件**：
   *
   *   `captain.completed` / `captain.failed` → `{kind:'terminal', reportId}`
   *      可以**停止等待**（durable 报告已落盘）。调用方按 `reportId` 回源
   *      `GET /captain/{reportId}`。
   *   `captain.quick_assessment`           → `{kind:'quick', quick}`
   *      **必须继续等待**（这只是第一段结果，Deep 仍在跑）。不回源 —— 此时
   *      GET 回来只有空报告；调用方直接渲染临时卡片。
   *
   * 为什么是**一个**回调而不是两个（Phase C.5）
   * -------------------------------------------
   * 归属规则必须由同一个 owner 持有。两个独立回调意味着「接受 B 的临时结果」
   * 时没有任何东西知道「A 已经落盘终态」，那份临时卡片就会挂到已经完成的 A 上。
   * 合并后，`useCaptainReferee` 内部持有 reportId / status 两个 ref，两条规则
   * （属于当前报告 / 当前不是终态）在一处判定，不存在二者不同步的窗口。
   *
   * 与 `onStateInvalidated` 一样，**不把 terminal payload 当内容**交给调用方：
   * `captain.completed` 虽然例外地携带了报告的一部分字段（positions /
   * dispute_points / uncertainty，后端为即时渲染刻意做的偏离），
   * 但它是完整报告的子集 —— 缺 `discussion_summary` / `agreement_points` /
   * `evidence` / `suggestion`。用它渲染会得到一份看起来完整、实则残缺的报告，
   * 所以调用方一律回源 `GET /captain/{report_id}`（payload 里始终有 report_id）。
   *
   * 存在 `Ref` 里，故意**不**进 effect 依赖 —— 同 onStateInvalidated 的理由：
   * 回调每次 render 换引用会重连 SSE。
   */
  onCaptainEvent?: (event: CaptainStreamEvent) => void
}

/**
 * 裁判事件的三态联合（Task 6 / Phase C.5 §5）。
 *
 * 判别式 `kind` 而不是把 `quick` 做成可选字段：可选字段会让「terminal 事件却带
 * 了 quick」和「quick 事件却没有 quick」两种非法组合都类型合法，调用方只能靠
 * 运行时 if 兜住 —— 这正是下面要防的错归因。
 */
export type CaptainStreamEvent =
  | { kind: 'terminal'; reportId: string | null }
  | { kind: 'quick'; quick: CaptainQuickEventPayload | null }

/** 从 partial 记录里摘掉某 utterance（无则原样返回，避免无谓 setState） */
function withoutPartial(
  partials: Record<string, PartialUtterance>,
  utteranceId: string,
): Record<string, PartialUtterance> {
  if (!(utteranceId in partials)) return partials
  const next = { ...partials }
  delete next[utteranceId]
  return next
}

export function useMeetingStream({
  meetingId,
  status,
  snapshotReady,
  maxSegmentSeq,
  onStateInvalidated,
  onCaptainEvent,
}: UseMeetingStreamParams) {
  const esRef = useRef<EventSource | null>(null)
  const timerRef = useRef<number | null>(null)
  const genRef = useRef(0)
  const retryRef = useRef(0)
  const maxSeqRef = useRef(0)
  const statusRef = useRef(status)
  // 失效回调经 Ref 读取：handler 闭包与 effect 依赖都不随回调引用变化（见参数注释）
  const onStateInvalidatedRef = useRef(onStateInvalidated)
  onStateInvalidatedRef.current = onStateInvalidated
  // 同上：裁判事件回调经 Ref 读取，不参与 effect 依赖（否则每次 render 重连 SSE）
  const onCaptainEventRef = useRef(onCaptainEvent)
  onCaptainEventRef.current = onCaptainEvent
  // Snapshot 游标只增不减：初始以此 seed；后续 live FINAL / 显式 recovery 更新。
  // 若每次 render 都覆盖会把「已收到的 live max」打回 snapshot 值，破坏重连游标。
  if (maxSegmentSeq > maxSeqRef.current) maxSeqRef.current = maxSegmentSeq
  statusRef.current = status

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

  /** writer helpers：SSE reducer 逻辑（FINAL 去重 + ASC；PARTIAL 原地覆盖） */
  const handlePartial = (env: SseEnvelope) => {
    const p = env.payload as { utterance_id?: string; speaker_id?: string; text?: string }
    if (!p?.utterance_id) return
    useMeetingStore.getState().upsertPartial({
      utterance_id: p.utterance_id,
      speaker_id: p.speaker_id ?? 'unknown',
      text: p.text ?? '',
    })
  }

  const handleFinal = (env: SseEnvelope) => {
    const p = env.payload as Record<string, unknown>
    if (typeof p.segment_seq !== 'number' || typeof p.utterance_id !== 'string') return
    const seg: TranscriptSegment = {
      segment_seq: p.segment_seq,
      utterance_id: p.utterance_id,
      speaker_id: typeof p.speaker_id === 'string' ? p.speaker_id : 'unknown',
      text: typeof p.text === 'string' ? p.text : '',
      start_ms: typeof p.start_ms === 'number' ? p.start_ms : null,
      end_ms: typeof p.end_ms === 'number' ? p.end_ms : null,
    }
    // segment_seq 是前端去重主依据（Snapshot catch-up / 重连重复均不重复展示）+ ASC
    useMeetingStore.setState((s) => {
      if (s.finalSegments.some((f) => f.segment_seq === seg.segment_seq)) {
        return { partialUtterances: withoutPartial(s.partialUtterances, seg.utterance_id) }
      }
      return {
        finalSegments: [...s.finalSegments, seg].sort(
          (a, b) => a.segment_seq - b.segment_seq,
        ),
        partialUtterances: withoutPartial(s.partialUtterances, seg.utterance_id),
      }
    })
    if (seg.segment_seq > maxSeqRef.current) maxSeqRef.current = seg.segment_seq
  }

  const handleMeetingStatus = async (mid: string) => {
    try {
      // Server 是 Source of Truth：状态事件只是通知，用 GET Meeting 覆盖
      const updated = await getMeeting(mid)
      useMeetingStore.getState().setMeeting(updated)
    } catch {
      // 拉取失败：保留现状不清空；后续状态事件会再触发
    }
  }

  const handleRecovery = (mid: string) => {
    const gen = genRef.current
    const attempt = retryRef.current
    retryRef.current += 1
    const delay = backoffDelay(attempt)
    if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    timerRef.current = window.setTimeout(async () => {
      timerRef.current = null
      if (genRef.current !== gen) return // 已清理（离开页面 / 切换会议）
      const st = statusRef.current
      if (st === 'CREATED' || st === 'COMPLETED') return // 不再需要实时
      try {
        // Snapshot Recovery：重拉已持久化 FINAL → 新游标 → 新 SSE
        const snapshot = await getTranscriptSnapshot(mid)
        if (genRef.current !== gen) return
        const segs = snapshot.segments
        useMeetingStore.getState().setFinalSegments(segs)
        const newMax = segs.length ? segs[segs.length - 1].segment_seq : 0
        maxSeqRef.current = newMax
        // meeting.state.updated is non-durable and may have been missed while
        // disconnected. Transcript snapshot recovery and AI-state recovery are
        // paired: GET /state remains authoritative, SSE only invalidates it.
        onStateInvalidatedRef.current?.()
        openStream(mid, newMax)
      } catch {
        if (genRef.current !== gen) return
        handleRecovery(mid) // Snapshot 失败 → 继续退避重试
      }
    }, delay)
  }

  const openStream = (mid: string, cursor: number) => {
    if (esRef.current) return // 单连接防护：任意时刻最多一个 EventSource
    const gen = genRef.current
    useMeetingStore.getState().setStreamStatus('connecting')

    const es = new EventSource(`/api/meetings/${mid}/events?after_segment_seq=${cursor}`)
    esRef.current = es

    es.onopen = () => {
      if (genRef.current !== gen) {
        es.close()
        return
      }
      retryRef.current = 0 // 成功连接后重置退避
      useMeetingStore.getState().setStreamStatus('connected')
    }

    es.onerror = () => {
      if (genRef.current !== gen) return
      if (esRef.current) {
        esRef.current.close()
        esRef.current = null
      }
      useMeetingStore.getState().setStreamStatus('reconnecting')
      useMeetingStore.getState().clearPartialUtterances() // PARTIAL 临时态，恢复前清空
      handleRecovery(mid)
    }

    // 后端发送**具名**帧（`event: transcript.final`），不会触发 onmessage；
    // 必须逐事件名订阅（见 utils/sse.ts）。
    subscribeSseEnvelopes(es, (env) => {
      if (genRef.current !== gen) return
      if (env.meeting_id !== mid) return // 防御：只处理本会议帧
      switch (env.event_type) {
        case 'transcript.partial':
          handlePartial(env)
          break
        case 'transcript.final':
          handleFinal(env)
          break
        case 'meeting.status':
          // eslint-disable-next-line @typescript-eslint/no-floating-promises
          handleMeetingStatus(mid)
          break
        case 'meeting.state.updated':
          // Step UI-INTEL-1：纯失效通知 → 重新 GET /state。
          // 后端保证「先 commit 后 publish」；事件本身 non-durable、不回放，
          // 所以这里只触发拉取，不消费 payload、不做版本比较（由 hook 守卫）。
          onStateInvalidatedRef.current?.()
          break
        case 'captain.completed':
        case 'captain.failed': {
          // ADR-002 Phase 2-A：裁判报告就绪 / 失败 → 回源读那一份报告。
          // 只把 report_id 传出去，**不**消费 payload 的报告片段（它是子集，
          // 渲染它会得到残缺报告 —— 见 onCaptainEvent 的参数注释）。
          // 成功与失败走同一条路径：两种情况下事实源都是 GET（失败时
          // report_json 为 NULL，GET 回来就是 status=FAILED，没有内容）。
          const p = env.payload as { report_id?: unknown }
          const reportId = typeof p.report_id === 'string' ? p.report_id : null
          onCaptainEventRef.current?.({ kind: 'terminal', reportId })
          break
        }
        case 'captain.quick_assessment': {
          // Task 6：两阶段裁判的第一段。**不**回源 GET —— 此时报告仍是
          // ANALYZING，GET 回来只有空报告；这一段的全部价值就是不等 Deep。
          // 与 terminal 事件的关键区别是**不能**停止 loading，所以走同一个
          // 回调的另一种 kind，由消费方（useCaptainReferee）按 ref 做归属判定
          // —— 归属规则必须与 terminal 的判定在同一处，见参数注释。
          const p = env.payload as { report_id?: unknown }
          if (typeof p.report_id !== 'string') {
            onCaptainEventRef.current?.({ kind: 'quick', quick: null }) // 防御：无主，不渲染
            break
          }
          onCaptainEventRef.current?.({
            kind: 'quick',
            quick: env.payload as unknown as CaptainQuickEventPayload,
          })
          break
        }
        default:
          break // heartbeat / summary.*（留给 8C）忽略
      }
    })
  }

  // ---- 主生命周期：连接条件满足才建立，否则关闭 ----
  useEffect(() => {
    if (!meetingId || !status) return
    const active = status === 'IN_PROGRESS' || status === 'FINALIZING'
    if (!active) {
      closeStream()
      useMeetingStore.getState().setStreamStatus('idle')
      return
    }
    if (!snapshotReady) return // Snapshot 前置：先恢复已持久化 FINAL 再连 SSE
    openStream(meetingId, maxSeqRef.current)
    return () => closeStream()
  }, [meetingId, status, snapshotReady])

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
