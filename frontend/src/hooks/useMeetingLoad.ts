import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { getMeeting, normalizeApiError } from '@/api'
import { useMeetingStore } from '@/stores/meetingStore'

/**
 * Meeting Room 页面加载钩子（8B-1）。
 *
 * 恢复模型（PRD §53 / §54 第一步）：GET Meeting →（8B-3 起）Snapshot → SSE。
 * 这里只做第一步；meetingId 变化时先 reset 清空 store，避免跨会议串数据，
 * 再请求。404 / network / 其它错误归一化为页面可直接渲染的轻量错误态。
 *
 * 组件不得直接调 axios —— 一律走 API 层 getMeeting。
 * reloadKey：错误「重试」时递增，触发重新 GET 同一 meeting（不重置 store，只清 error）。
 */
export function useMeetingLoad(
  meetingId: string | undefined,
  reloadKey = 0,
) {
  const navigate = useNavigate()
  const setMeeting = useMeetingStore((s) => s.setMeeting)
  const setLoading = useMeetingStore((s) => s.setLoading)
  const setError = useMeetingStore((s) => s.setError)
  const resetMeetingState = useMeetingStore((s) => s.resetMeetingState)

  useEffect(() => {
    if (!meetingId) {
      navigate('/')
      return
    }

    let cancelled = false
    // meetingId 变化 → 先清除旧会议状态，避免跨会议串数据
    resetMeetingState()
    setLoading(true)

    getMeeting(meetingId)
      .then((meeting) => {
        if (cancelled) return
        setMeeting(meeting)
      })
      .catch((raw: unknown) => {
        if (cancelled) return
        const error = normalizeApiError(raw)
        if (error.kind === 'http' && error.status === 404) {
          setError('not_found')
        } else if (error.kind === 'network' || (error.kind === 'http' && (error.status ?? 0) >= 500)) {
          // 断网 / 5xx（含 proxy 不可达时的 502/503）同样视为「后端不可达」
          setError('network')
        } else {
          setError(error.message || '会议加载失败')
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [meetingId, reloadKey, navigate, resetMeetingState, setMeeting, setLoading, setError])
}
