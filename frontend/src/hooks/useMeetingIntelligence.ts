import { useCallback, useEffect, useRef, useState } from 'react'
import { getMeetingState, normalizeApiError } from '@/api'
import type { MeetingState } from '@/types/meetingState'

export interface MeetingIntelligenceResult {
  /** 服务端持久化的 Meeting State（工作记忆）；未加载成功前为 null */
  state: MeetingState | null
  loading: boolean
  /**
   * 错误归一化（喂给展示层，不暴露技术细节）：
   *  - ''        无错误
   *  - 'not_found' 会议不存在（后端 404）
   *  - 'network'   网络 / 5xx
   *  - 其它        具体错误消息
   */
  error: string
  /**
   * 重新 GET 一次 Meeting State（**不返回数据**，结果直接落到 hook state）。
   *
   * 调用方：① 面板手动「重试 / 刷新」；② `useMeetingStream` 收到
   * `meeting.state.updated` 失效通知后回调。两者共用同一份去重 / 版本守卫。
   */
  refreshState: () => void
}

/**
 * Meeting Intelligence 数据钩子（Step UI-INTEL-1）。
 *
 * 数据模型：`GET /api/meetings/:id/state` 是 Meeting State 的**唯一事实源**；
 * `meeting.state.updated` 只是**失效通知**（non-durable、不回放、不带内容），
 * 收到后重新 GET —— 绝不把 SSE payload 当内容渲染（payload 只有 {version,status}）。
 *
 * 三条硬约束：
 *  1. **只读**：本 hook 绝不从 Transcript 重新推导 decision / action item /
 *     risk。展示层拿到的每一个条目都来自服务端 Meeting State。
 *  2. **不改全局 store**：状态留在 hook 内部（Meeting Room 页面作用域），
 *     不扩 Zustand 全局 store —— Meeting State 与 Transcript 是两个数据域。
 *  3. **不影响其它链路**：失败只让本面板降级，绝不影响实时字幕 / start /
 *     finish / summary（本 hook 不做任何写操作）。
 *
 * 两道守卫（缺一不可）：
 *   - **request sequence**：只有最后一次发起的请求可以落 state，过期响应直接丢弃
 *     （乱序返回时旧响应不得覆盖新响应）。
 *   - **version**：`incoming.version >= current.version` 才允许落 state。
 *     首个 `version = 0 / status = EMPTY` 必须允许正常进入（初始基线为 -1），
 *     否则「尚未处理任何 FINAL」这一最常见早期状态会被误判为过期。
 *     注意 FAILED **不推进 version**（见后端 mark_failed），所以判定必须是
 *     `>=` 而非 `>` —— 否则同一 version 下的 ACTIVE → FAILED 会被静默丢掉。
 *
 * 触发时机：meetingId 变化 / 显式 refreshState（手动或 SSE 失效）。
 * 无轮询、无定时器。
 */
export function useMeetingIntelligence(
  meetingId: string | undefined,
  enabled: boolean,
): MeetingIntelligenceResult {
  const [state, setState] = useState<MeetingState | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  /** 请求序号：每次 refreshState / meetingId 变化 +1，过期响应比对后丢弃 */
  const seqRef = useRef(0)
  /** 已落地的最大 version 基线；-1 保证首个 version=0 能进入 */
  const versionRef = useRef(-1)
  /** meetingId 变化后作废旧响应（与 seqRef 一起构成完整迟到防护） */
  const meetingRef = useRef<string | undefined>(meetingId)

  const fetchState = useCallback(
    (mid: string) => {
      seqRef.current += 1
      const seq = seqRef.current
      setLoading(true)
      setError('')

      getMeetingState(mid)
        .then((data) => {
          // 守卫 1：只有最后一次请求可以落 state（乱序返回的旧响应丢弃）
          if (seq !== seqRef.current || mid !== meetingRef.current) return
          // 守卫 2：version 只能前进（含持平 —— FAILED 不推进 version）
          if (data.version < versionRef.current) return
          versionRef.current = data.version
          setState(data)
        })
        .catch((raw: unknown) => {
          if (seq !== seqRef.current || mid !== meetingRef.current) return
          // 失败不清空已展示的 last-good 内容（见面板：保留内容 + 轻量不可用提示）
          const normalized = normalizeApiError(raw)
          if (normalized.kind === 'http' && normalized.status === 404) {
            setError('not_found')
          } else if (
            normalized.kind === 'network' ||
            (normalized.kind === 'http' && (normalized.status ?? 0) >= 500)
          ) {
            setError('network')
          } else {
            setError(normalized.message || '会议状态加载失败')
          }
        })
        .finally(() => {
          if (seq === seqRef.current && mid === meetingRef.current) setLoading(false)
        })
    },
    [],
  )

  // meetingId 变化 / 未就绪 → 整体归零，避免跨会议串数据
  useEffect(() => {
    meetingRef.current = meetingId
    seqRef.current += 1 // 作废上一会议在飞的请求
    versionRef.current = -1
    setState(null)
    setError('')
    setLoading(false)

    if (!meetingId || !enabled) return
    fetchState(meetingId)
  }, [meetingId, enabled, fetchState])

  const refreshState = useCallback(() => {
    const mid = meetingRef.current
    if (!mid) return
    fetchState(mid)
  }, [fetchState])

  return { state, loading, error, refreshState }
}
