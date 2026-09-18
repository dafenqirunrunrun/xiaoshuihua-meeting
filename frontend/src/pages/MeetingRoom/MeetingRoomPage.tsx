import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Button, Skeleton, Tag, message, Popconfirm } from 'antd'
import {
  ClockCircleOutlined,
  AudioOutlined,
  ReloadOutlined,
  ArrowLeftOutlined,
  PlayCircleOutlined,
  StopOutlined,
} from '@ant-design/icons'
import { EmptyPlaceholder } from '@/components/EmptyPlaceholder'
import { InfoRow } from '@/components/InfoRow'
import { MeetingStatusTag } from '@/components/MeetingStatusTag'
import { useMeetingStore } from '@/stores/meetingStore'
import { useMeetingLoad } from '@/hooks/useMeetingLoad'
import { useTranscriptSnapshot } from '@/hooks/useTranscriptSnapshot'
import { formatDateTime, formatDuration, elapsedMs } from '@/utils/time'
import { startMeeting, finishMeeting, normalizeApiError } from '@/api'
import type { Meeting } from '@/types/meeting'
import type { TranscriptSegment } from '@/types/transcript'

/** 实时秒数刷新 hook：仅 active 时以 1s 步进更新，unmount 或 active 变 false 即清理。 */
function useNowTick(active: boolean, startIso: string | null | undefined) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active || !startIso) return
    setNow(Date.now())
    const t = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(t)
  }, [active, startIso])
  return now
}

/** Header strip：真实后端字段，title 缺失/为空时才 fallback「未命名会议」。 */
function Header({ meeting, action }: { meeting: Meeting; action?: React.ReactNode }) {
  const title = meeting.title?.trim() || '未命名会议'
  const startedAtText = meeting.started_at
    ? `${formatDateTime(meeting.started_at, true)} 开始`
    : '等待开始'
  return (
    <header className="room__header">
      <div style={{ minWidth: 0 }}>
        <h1 className="room__title h-m0" title={meeting.title || undefined}>
          {title}
        </h1>
        <div className="room__meta" style={{ marginTop: 6 }}>
          {meeting.id ? <span className="room__id">#{meeting.id}</span> : null}
          {meeting.id ? <span className="meta-sep" /> : null}
          <span>{startedAtText}</span>
          <span className="meta-sep" />
          <MeetingStatusTag status={meeting.status} />
        </div>
      </div>
      {action ? <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>{action}</div> : null}
    </header>
  )
}

/** 主区占位文案（四态）：8B-3 无 FINAL 时的中性空态，8B-4 才挂 PARTIAL 实时字幕。 */
function transcriptPlaceholder(status: Meeting['status']) {
  switch (status) {
    case 'IN_PROGRESS':
      return {
        title: '等待语音输入...',
        description: '',
      }
    case 'FINALIZING':
      return {
        title: '暂无已确认的转写内容',
        description: '会议正在收尾，等待最后的转写内容确认。',
      }
    case 'COMPLETED':
      return {
        title: '暂无可展示的转写内容',
        description: '本次会议未流转写出已确认的 FINAL 内容。',
      }
    default:
      return { title: '', description: '' }
  }
}

/** 便于阅读的时长偏移：无起止时间则隐藏，不制造时间戳（8B-3 只展示真实字段）。 */
function timingText(startMs: number | null, endMs: number | null): string {
  if (startMs === null || endMs === null) return ''
  return `${(startMs / 1000).toFixed(1)}s`
}

/**
 * 转写内容区：展示已持久化 FINAL，或按状态的中性空态 / 轻量失败态。
 * 8B-3 不做 PARTIAL / streaming 光标 / 自动滚动。
 */
function TranscriptBody({
  status,
  segments,
  error,
  loading,
  onRetry,
}: {
  status: Meeting['status']
  segments: TranscriptSegment[]
  error: string
  loading: boolean
  onRetry: () => void
}) {
  const placeholder = transcriptPlaceholder(status)

  if (loading && segments.length === 0) {
    return (
      <div className="transcript-surface transcript-surface--top">
        <Skeleton active paragraph={{ rows: 3 }} title={false} />
      </div>
    )
  }

  if (error) {
    return (
      <div className="transcript-surface">
        <EmptyPlaceholder
          icon={<ReloadOutlined />}
          title="转写记录加载失败"
          description={error}
          action={
            <Button size="small" icon={<ReloadOutlined />} onClick={onRetry}>
              重试
            </Button>
          }
          compact
        />
      </div>
    )
  }

  if (segments.length === 0) {
    return (
      <div className="transcript-surface">
        <EmptyPlaceholder
          icon={<AudioOutlined />}
          title={placeholder.title}
          description={placeholder.description}
          compact
        />
      </div>
    )
  }

  return (
    <div className="transcript-surface transcript-surface--top">
      <ol className="transcript-segments">
        {segments.map((seg) => (
          <li key={seg.segment_seq} className="transcript-segment">
            <span className="transcript-segment__speaker">{seg.speaker_id}</span>
            <span className="transcript-segment__text">{seg.text}</span>
            {timingText(seg.start_ms, seg.end_ms) ? (
              <span className="transcript-segment__time">
                {timingText(seg.start_ms, seg.end_ms)}
              </span>
            ) : null}
          </li>
        ))}
      </ol>
    </div>
  )
}

/** AI 纪要标签（仅展示，不请求 summary 接口） */
function summaryTag(status: Meeting['status']) {
  if (status === 'COMPLETED') {
    return <Tag color="green">已完成</Tag>
  }
  if (status === 'FINALIZING') {
    return <Tag color="processing">正在生成</Tag>
  }
  return <Tag>等待会议结束</Tag>
}

/** 转写标签（仅展示） */
function transcriptTag(status: Meeting['status']) {
  if (status === 'IN_PROGRESS') return <Tag color="blue">进行中</Tag>
  if (status === 'FINALIZING' || status === 'COMPLETED') return <Tag>已关闭</Tag>
  return <Tag color="default">等待会议开始</Tag>
}

/**
 * Meeting Room —— Step 8B-1 真实 GET Meeting 驱动 + 8B-2 生命周期（start/finish）
 * + 8B-3 Transcript Snapshot（已持久化 FINAL 恢复展示）。
 * 创建 ≠ 开始：CREATED 展示「准备开始会议」，点击「开始会议」→ POST /start → IN_PROGRESS；
 * IN_PROGRESS 可「结束会议」→ POST /finish → FINALIZING。
 * Server 是 Source of Truth：start/finish 均以 backend 返回的 Meeting 覆盖 store，
 * 前端绝不自行写 status / started_at / COMPLETED。
 * 8B-3：Meeting 成功后 GET /transcript Snapshot 恢复 FINAL 列表（ASC，仅历史事实）。
 * 8B-4 才接入 SSE 实时转写；此处不调用 summary 接口。
 */
export function MeetingRoomPage() {
  const { meetingId } = useParams<{ meetingId: string }>()
  const navigate = useNavigate()

  const meeting = useMeetingStore((s) => s.meeting)
  const loading = useMeetingStore((s) => s.loading)
  const error = useMeetingStore((s) => s.error)
  const setError = useMeetingStore((s) => s.setError)
  const setMeeting = useMeetingStore((s) => s.setMeeting)

  // 重试：bump reloadKey → hook 重新 GET 同一 meeting
  const [reloadKey, setReloadKey] = useState(0)
  useMeetingLoad(meetingId, reloadKey)

  // 8B-3：Meeting 成功存在后再加载 Transcript Snapshot（仅持久化 FINAL，ASC）
  const finalSegments = useMeetingStore((s) => s.finalSegments)
  const {
    transcriptError,
    transcriptLoading,
    retryTranscript,
  } = useTranscriptSnapshot(
    meetingId,
    !!meeting && meeting.status !== 'CREATED',
    meeting?.status,
  )

  const now = useNowTick(
    meeting?.status === 'IN_PROGRESS',
    meeting?.started_at ?? null,
  )
  const durationText =
    meeting?.status === 'IN_PROGRESS' && meeting.started_at
      ? formatDuration(elapsedMs(meeting.started_at, new Date(now)))
      : '--'

  // ---- 生命周期动作（Step 8B-2）：start / finish，仅经 API layer ----
  const [startLoading, setStartLoading] = useState(false)
  const [finishLoading, setFinishLoading] = useState(false)

  const handleStart = async () => {
    if (!meetingId) return
    setStartLoading(true)
    try {
      const updated = await startMeeting(meetingId)
      // Server 是 Source of Truth：用 backend 返回的 Meeting 覆盖 store
      setMeeting(updated)
    } catch (raw: unknown) {
      // 失败：保留当前状态，展示后端语义（409 detail 等），按钮恢复可点
      message.error(normalizeApiError(raw).message)
    } finally {
      setStartLoading(false)
    }
  }

  const handleFinish = async () => {
    if (!meetingId) return
    setFinishLoading(true)
    try {
      const updated = await finishMeeting(meetingId)
      setMeeting(updated)
    } catch (raw: unknown) {
      message.error(normalizeApiError(raw).message)
    } finally {
      setFinishLoading(false)
    }
  }

  // 首次加载（且尚未拿到 meeting 或错误）→ 轻量 Skeleton，不闪现 demo 值
  if (loading && !meeting && !error) {
    return (
      <div className="page page--wide" style={{ minHeight: 0 }}>
        <Skeleton active paragraph={{ rows: 1 }} title={{ width: 240 }} />
        <div style={{ marginTop: 20 }}>
          <Skeleton active paragraph={{ rows: 6 }} />
        </div>
      </div>
    )
  }

  // 404 → 明确「不存在」而非 fake
  if (error === 'not_found') {
    return (
      <div className="page page--wide" style={{ minHeight: 0 }}>
        <div className="panel" style={{ padding: 32 }}>
          <EmptyPlaceholder
            icon={<AudioOutlined />}
            title="会议不存在或已被删除"
            description="请确认链接是否正确，或返回首页创建新会议。"
            action={
              <Button type="primary" icon={<ArrowLeftOutlined />} onClick={() => navigate('/')}>
                返回首页
              </Button>
            }
          />
        </div>
      </div>
    )
  }

  // network / 5xx / 其它 → 明确「加载失败」+ 重试（只重新 GET meeting）
  if (error && error !== 'not_found') {
    return (
      <div className="page page--wide" style={{ minHeight: 0 }}>
        <div className="panel" style={{ padding: 32 }}>
          <EmptyPlaceholder
            icon={<ReloadOutlined />}
            title="会议加载失败"
            description={
              error === 'network'
                ? '无法连接后端服务，请确认服务已启动后重试。'
                : '加载会议时出现问题，请稍后重试。'
            }
            action={
              <Button
                type="primary"
                icon={<ReloadOutlined />}
                onClick={() => {
                  setError('')
                  setReloadKey((k) => k + 1)
                }}
              >
                重试
              </Button>
            }
          />
        </div>
      </div>
    )
  }

  if (!meeting) return null

  // ---- CREATED：会前准备 + 真实「开始会议」（Step 8B-2 启用） ----
  if (meeting.status === 'CREATED') {
    return (
      <div className="page page--wide" style={{ minHeight: 0 }}>
        <Header
          meeting={meeting}
          action={
            <Button
              type="primary"
              icon={<PlayCircleOutlined />}
              loading={startLoading}
              onClick={handleStart}
            >
              开始会议
            </Button>
          }
        />

        <div style={{ marginTop: 24 }}>
          <EmptyPlaceholder
            icon={<ClockCircleOutlined />}
            title="准备开始会议"
            description="开始会议后，小水华将实时记录会议内容，并在会议结束后自动生成纪要。"
          />
        </div>

        <aside className="room__rail" style={{ marginTop: 24 }}>
          <div className="rail-section">
            <div className="rail-title">会议状态</div>
            <InfoRow label="会议状态" value={<MeetingStatusTag status="CREATED" />} />
            <InfoRow label="开始时间" value="--" />
            <InfoRow label="持续时长" value="--" />
          </div>
          <div className="rail-section">
            <div className="rail-title">转写与纪要</div>
            <InfoRow label="转写状态" value={<Tag>等待会议开始</Tag>} />
            <InfoRow label="AI 纪要" value={<Tag>等待会议结束</Tag>} />
          </div>
          <div className="rail-section">
            <div className="rail-title">会议信息</div>
            <InfoRow label="Meeting ID" value={<span className="room__id">{meeting.id}</span>} />
          </div>
        </aside>
      </div>
    )
  }

  // ---- 其它状态：IN_PROGRESS / FINALIZING / COMPLETED ----
  // 四态动作严格：只有 IN_PROGRESS 显示「结束会议」；FINALIZING / COMPLETED 无 start/finish
  const headerAction =
    meeting.status === 'IN_PROGRESS' ? (
      <Popconfirm
        title="结束会议后将进入收尾阶段，是否继续？"
        okText="结束会议"
        cancelText="取消"
        okButtonProps={{ danger: true, loading: finishLoading }}
        onConfirm={handleFinish}
      >
        <Button type="primary" danger icon={<StopOutlined />} loading={finishLoading}>
          结束会议
        </Button>
      </Popconfirm>
    ) : undefined

  return (
    <div className="page page--wide" style={{ minHeight: 0 }}>
      <Header meeting={meeting} action={headerAction} />

      <div className="room__body">
        <section className="room__transcript">
          <div className="room__section-head">
            <span className="live-dot" />
            <span>实时记录</span>
          </div>
          <TranscriptBody
            status={meeting.status}
            segments={finalSegments}
            error={transcriptError}
            loading={transcriptLoading}
            onRetry={retryTranscript}
          />
        </section>

        <aside className="room__rail">
          <div className="rail-section">
            <div className="rail-title">状态</div>
            <InfoRow label="会议状态" value={<MeetingStatusTag status={meeting.status} />} />
            <InfoRow
              label="开始时间"
              value={meeting.started_at ? formatDateTime(meeting.started_at, true) : '--'}
            />
            <InfoRow label="持续时长" value={durationText} />
          </div>

          <div className="rail-section">
            <div className="rail-title">AI 纪要</div>
            <InfoRow label="纪要状态" value={summaryTag(meeting.status)} />
            <InfoRow
              label="纪要说明"
              value={
                <span style={{ fontSize: 12, color: '#98A2B3' }}>
                  结束会议后自动生成
                </span>
              }
            />
          </div>

          <div className="rail-section">
            <div className="rail-title">会议信息</div>
            <InfoRow label="Meeting ID" value={<span className="room__id">{meeting.id}</span>} />
            <InfoRow label="转写" value={transcriptTag(meeting.status)} />
          </div>
        </aside>
      </div>
    </div>
  )
}
