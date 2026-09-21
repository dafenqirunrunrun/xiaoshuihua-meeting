import { useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Button, Skeleton, Tag, message, Popconfirm } from 'antd'
import {
  AudioOutlined,
  ReloadOutlined,
  ArrowLeftOutlined,
  PlayCircleOutlined,
  StopOutlined,
  ArrowDownOutlined,
  CheckOutlined,
  ClockCircleOutlined,
  FileTextOutlined,
  BulbOutlined,
  WifiOutlined,
} from '@ant-design/icons'
import { EmptyPlaceholder } from '@/components/EmptyPlaceholder'
import { MeetingIntelligencePanel } from '@/components/MeetingIntelligencePanel'
import { MeetingStatusTag } from '@/components/MeetingStatusTag'
import { useMeetingStore } from '@/stores/meetingStore'
import { useMeetingLoad } from '@/hooks/useMeetingLoad'
import { useMeetingIntelligence } from '@/hooks/useMeetingIntelligence'
import { useTranscriptSnapshot } from '@/hooks/useTranscriptSnapshot'
import { useMeetingStream } from '@/hooks/useMeetingStream'
import { useAutoScroll } from '@/hooks/useAutoScroll'
import { useTranscriptEvidenceNavigation } from '@/hooks/useTranscriptEvidenceNavigation'
import { formatDuration, elapsedMs } from '@/utils/time'
import { startMeeting, finishMeeting, normalizeApiError } from '@/api'
import type { Meeting } from '@/types/meeting'
import type { PartialUtterance, TranscriptSegment } from '@/types/transcript'

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

/**
 * Workspace header：会议身份、核心运行状态和主要操作形成一个整体。
 * 左上角提供纯导航「返回会议列表」：离开页面 ≠ 结束/暂停会议（Step 8B-6 §16）。
 */
function Header({
  meeting,
  action,
  timer,
  onBack,
  details,
}: {
  meeting: Meeting
  action?: React.ReactNode
  timer?: string
  onBack?: () => void
  details?: React.ReactNode
}) {
  const title = meeting.title?.trim() || '未命名会议'
  return (
    <header className="room__header">
      <div className="room__header-main">
        <div className="room__header-identity">
          {onBack ? (
            <button type="button" className="room__back" onClick={onBack}>
              <ArrowLeftOutlined aria-hidden />
              返回会议列表
            </button>
          ) : null}
          <div className="room__title-row">
            <h1 className="room__title h-m0" title={meeting.title || undefined}>
              {title}
            </h1>
            <MeetingStatusTag status={meeting.status} />
          </div>
          <div className="room__meta">
            {meeting.id ? <span className="room__id">#{meeting.id}</span> : null}
            {timer ? (
              <>
                <span className="meta-sep" />
                <span className="u-tabular">{timer}</span>
              </>
            ) : null}
          </div>
        </div>
        {action ? <div className="room__header-action">{action}</div> : null}
      </div>
      {details ? <div className="room__status-strip">{details}</div> : null}
    </header>
  )
}

function HeaderMetric({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode
  label: string
  value: React.ReactNode
}) {
  return (
    <div className="room-metric">
      <span className="room-metric__icon" aria-hidden>{icon}</span>
      <span className="room-metric__copy">
        <span>{label}</span>
        <strong>{value}</strong>
      </span>
    </div>
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

/** 便于阅读的时长偏移：无起止时间则隐藏，不制造时间戳（只展示真实字段）。 */
function timingText(startMs: number | null, endMs: number | null): string {
  if (startMs === null || endMs === null) return ''
  return `${(startMs / 1000).toFixed(1)}s`
}

interface TranscriptGroup {
  speakerId: string
  segments: TranscriptSegment[]
}

/** Consecutive FINALs from one speaker are one visual message, while every
 * segment remains in the DOM as an evidence-addressable record. */
function groupTranscriptSegments(segments: TranscriptSegment[]): TranscriptGroup[] {
  return segments.reduce<TranscriptGroup[]>((groups, segment) => {
    const current = groups[groups.length - 1]
    if (current?.speakerId === segment.speaker_id) {
      current.segments.push(segment)
      return groups
    }
    groups.push({ speakerId: segment.speaker_id, segments: [segment] })
    return groups
  }, [])
}

function speakerLabel(speakerId: string): string {
  const normalized = speakerId.trim()
  const generic = normalized.match(/^speaker[_\s-]?(\d+)$/i)
  if (generic) return `发言人 ${generic[1]}`
  return normalized || '未知发言人'
}

function speakerInitial(speakerId: string): string {
  const label = speakerLabel(speakerId)
  const generic = label.match(/^发言人\s*(\d+)$/)
  return generic ? generic[1].slice(-2) : label.slice(0, 1).toUpperCase()
}

function speakerTone(speakerId: string): number {
  let hash = 0
  for (const character of speakerId) hash = (hash * 31 + character.charCodeAt(0)) >>> 0
  return hash % 6
}

/** 实时转写状态标签：meeting.status 与 streamStatus 是两个不同维度。 */
function streamStatusTag(
  meetingStatus: Meeting['status'],
  streamStatus: ReturnType<typeof useMeetingStore.getState>['streamStatus'],
) {
  switch (meetingStatus) {
    case 'CREATED':
      return <Tag color="default">等待会议开始</Tag>
    case 'IN_PROGRESS':
      if (streamStatus === 'connected') return <Tag color="success">实时转写已连接</Tag>
      if (streamStatus === 'reconnecting') return <Tag color="warning">重新连接中</Tag>
      if (streamStatus === 'connecting') return <Tag color="processing">连接中...</Tag>
      return <Tag color="default">等待实时连接</Tag>
    case 'FINALIZING':
      if (streamStatus === 'connected') return <Tag color="success">正在接收最后转写</Tag>
      return <Tag color="default">正在收尾</Tag>
    case 'COMPLETED':
      return <Tag>转写已结束</Tag>
    default:
      return <Tag color="default">等待会议开始</Tag>
  }
}

/**
 * 转写内容区：已持久化 FINAL（正式稳定）+ 实时 PARTIAL（次级轻量）+ 空态 / 失败态。
 * meeting-notes 风格（非聊天气泡）：speaker 弱化小字，PARTIAL 带小薄荷 live 点。
 */
function TranscriptBody({
  status,
  segments,
  partials,
  streamStatus,
  error,
  loading,
  onRetry,
  listRef,
}: {
  status: Meeting['status']
  segments: TranscriptSegment[]
  partials: PartialUtterance[]
  streamStatus: ReturnType<typeof useMeetingStore.getState>['streamStatus']
  error: string
  loading: boolean
  onRetry: () => void
  listRef?: React.Ref<HTMLOListElement>
}) {
  const placeholder = transcriptPlaceholder(status)
  const hasContent = segments.length > 0 || partials.length > 0
  const reconnecting = streamStatus === 'reconnecting' || streamStatus === 'connecting'
  const groups = groupTranscriptSegments(segments)

  if (loading && segments.length === 0 && partials.length === 0) {
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

  if (!hasContent) {
    // 空态文案按状态 + 断线恢复提示
    const title =
      status === 'IN_PROGRESS' && reconnecting
        ? '实时连接恢复中...'
        : placeholder.title
    return (
      <div className="transcript-surface">
        <EmptyPlaceholder
          icon={<AudioOutlined />}
          title={title}
          description={placeholder.description}
          compact
        />
      </div>
    )
  }

  return (
    <div className="transcript-surface transcript-surface--top">
      <ol className="transcript-segments transcript-chat" ref={listRef}>
        {groups.map((group) => {
          const first = group.segments[0]
          return (
            <li key={first.segment_seq} className="transcript-message-group">
              <span
                className="transcript-avatar"
                data-avatar-tone={speakerTone(group.speakerId)}
                aria-hidden
              >
                {speakerInitial(group.speakerId)}
              </span>
              <div className="transcript-message">
                <div className="transcript-message__meta">
                  <span className="transcript-message__speaker">
                    {speakerLabel(group.speakerId)}
                  </span>
                  {timingText(first.start_ms, first.end_ms) ? (
                    <span className="transcript-message__time">
                      {timingText(first.start_ms, first.end_ms)}
                    </span>
                  ) : null}
                </div>
                <div className="transcript-message__bubble">
                  {group.segments.map((seg) => (
                    <div
                      key={seg.segment_seq}
                      className="transcript-segment"
                      /* segment_seq remains the sole evidence navigation key. */
                      data-segment-seq={seg.segment_seq}
                    >
                      <span className="transcript-segment__text">{seg.text}</span>
                    </div>
                  ))}
                </div>
              </div>
            </li>
          )
        })}
        {partials.map((p) => (
          <li
            key={p.utterance_id}
            className="transcript-message-group transcript-message-group--partial"
          >
            <span
              className="transcript-avatar transcript-avatar--live"
              data-avatar-tone={speakerTone(p.speaker_id)}
              aria-hidden
            >
              {speakerInitial(p.speaker_id)}
            </span>
            <div className="transcript-message">
              <div className="transcript-message__meta">
                <span className="transcript-message__speaker">{speakerLabel(p.speaker_id)}</span>
                <span className="transcript-message__live">
                  <AudioOutlined aria-hidden />
                  正在说
                </span>
              </div>
              <div className="transcript-message__bubble transcript-message__bubble--partial">
                <div className="transcript-segment transcript-segment--partial">
                  <span className="transcript-segment__text">{p.text}</span>
                  <span className="transcript-typing" aria-hidden>
                    <i />
                    <i />
                    <i />
                  </span>
                </div>
              </div>
            </div>
          </li>
        ))}
      </ol>
    </div>
  )
}

/**
 * Meeting Room —— Step 8B-1 真实 GET Meeting 驱动 + 8B-2 生命周期（start/finish）
 * + 8B-3 Transcript Snapshot（已持久化 FINAL 恢复展示）+ 8B-4 实时 SSE + 自动滚动
 * + 8B-5 Visual Polish（结构 = Header → 主工作区(转写面板 + 右信息轨)）。
 * 创建 ≠ 开始：CREATED 展示「准备开始会议」（圆形麦克风 + ripple），点击「开始会议」
 * → POST /start → IN_PROGRESS；IN_PROGRESS 可「结束会议」→ POST /finish → FINALIZING。
 * Server 是 Source of Truth：start/finish 均以 backend 返回的 Meeting 覆盖 store，
 * 前端绝不自行写 status / started_at / COMPLETED。
 * 恢复模型：GET Meeting → GET Transcript Snapshot → maxSeq → SSE after_segment_seq。
 * SSE 只是实时通知通道，不是事实源（断 ≠ 丢；刷新走 Snapshot 恢复）。
 * 8B-5 只改展示层：数据逻辑（hooks / store / SSE）原样保留。
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
  const partials = useMeetingStore((s) => s.partialUtterances)
  const streamStatus = useMeetingStore((s) => s.streamStatus)
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
  const durationText = meeting?.started_at
    ? formatDuration(
        elapsedMs(
          meeting.started_at,
          meeting.ended_at ? new Date(meeting.ended_at) : new Date(now),
        ),
      )
    : '--'

  // 8B-4：Snapshot 成功后计算 maxSegmentSeq 作为 SSE 游标（无 FINAL → 0，backend 允许）
  const snapshotReady = !transcriptLoading && !transcriptError
  const maxSegmentSeq = finalSegments.length
    ? finalSegments[finalSegments.length - 1].segment_seq
    : 0

  // UI-INTEL-1：Meeting State（AI 工作记忆）。独立数据域、独立错误域 ——
  // 数据留在 hook 内（不进全局 store），失败只降级本面板，绝不影响转写 / 生命周期。
  // 不连流时（CREATED / COMPLETED）不拉取：CREATED 还没有内容，COMPLETED 属归档态。
  const intelligence = useMeetingIntelligence(
    meetingId,
    !!meeting && meeting.status !== 'CREATED',
  )

  // 8B-4：SSE 流（含 Snapshot-first 连接、断线恢复、meeting.status→GET Meeting）
  // UI-INTEL-1：**复用同一条 /events 连接**承接 meeting.state.updated 失效通知，
  // 收到后 refreshState() 重新 GET /state —— 不新增第二条 EventSource。
  useMeetingStream({
    meetingId,
    status: meeting?.status,
    snapshotReady,
    maxSegmentSeq,
    onStateInvalidated: intelligence.refreshState,
  })

  // 8B-4：自动滚动（跟随底部 / 向上查历史暂停 / 回到底部 FAB）
  const segmentsRef = useRef<HTMLOListElement>(null)
  // contentKey：FINAL 数 + PARTIAL 文本变化 → 驱动跟随（新行与正文变长都算）
  const partialList = Object.values(partials)
  const contentKey = `${finalSegments.length}:${partialList
    .map((p) => `${p.utterance_id}=${p.text}`)
    .join('|')}`
  const { showJumpButton, jumpToBottom, pauseAutoFollow } = useAutoScroll(
    segmentsRef,
    contentKey,
  )

  // UI-INTEL-2A：evidence 定位（「查看原文」→ 滚动 + 短暂高亮）。
  // contentSettled = Snapshot 加载已结束（成功或失败）—— 用于区分「还没等到」
  // 与「压根没有转写」，避免空会议时提示永久停在「正在加载原始记录…」。
  const evidence = useTranscriptEvidenceNavigation(
    segmentsRef,
    !transcriptLoading,
  )

  /**
   * 点击「查看原文」：**先暂停跟随，再定位**（§10 冻结顺序）。
   *
   * 顺序不能倒：先滚再 pause 的话，smooth 滚动途中若恰好有新 FINAL 到达，
   * auto-scroll 仍处于跟随态，会把用户直接拽回底部 —— 表现就是「点了没反应」。
   * 先 pause 则新内容进列表但不抢滚动，用户停在证据处；只有显式点「回到最新」
   * 才恢复跟随。
   */
  const handleTraceEvidence = (sourceSegmentSeqs: number[]) => {
    pauseAutoFollow()
    evidence.traceEvidence(sourceSegmentSeqs)
  }

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

  const processedSegmentSeq = intelligence.state?.last_processed_segment_seq ?? 0
  const pendingSegmentCount = finalSegments.filter(
    (segment) => segment.segment_seq > processedSegmentSeq,
  ).length
  const intelligenceMetric = intelligence.loading
    ? '正在同步'
    : pendingSegmentCount > 0 && meeting.status === 'IN_PROGRESS'
      ? `待整理 ${pendingSegmentCount} 条`
      : intelligence.state?.status === 'FROZEN'
        ? '已归档'
        : intelligence.state?.status === 'FAILED'
          ? '更新中断'
          : intelligence.state?.status === 'STALE'
            ? '更新稍有延迟'
            : intelligence.state?.status === 'ACTIVE'
              ? '已跟进最新内容'
              : '正在倾听'

  const headerDetails = (
    <>
      <HeaderMetric
        icon={<WifiOutlined />}
        label="实时转写"
        value={streamStatusTag(meeting.status, streamStatus)}
      />
      <HeaderMetric
        icon={<BulbOutlined />}
        label="AI 理解"
        value={intelligenceMetric}
      />
      <HeaderMetric
        icon={<ClockCircleOutlined />}
        label="会议时长"
        value={<span className="u-tabular">{durationText}</span>}
      />
      <HeaderMetric
        icon={<FileTextOutlined />}
        label="会议纪要"
        value={
          meeting.status === 'COMPLETED'
            ? '已生成'
            : meeting.status === 'FINALIZING'
              ? '正在生成'
              : '会后自动生成'
        }
      />
    </>
  )

  // ---- CREATED：会前准备（主工作区 + 右轨），真实「开始会议」（Step 8B-2 启用） ----
  if (meeting.status === 'CREATED') {
    return (
      <div className="page page--wide" style={{ minHeight: 0 }}>
        <Header
          meeting={meeting}
          onBack={() => navigate('/meetings')}
          details={headerDetails}
        />

        <div className="room__body room__body--prep">
          <section className="room__transcript">
            <div className="room__prep">
              <div className="prep-orb">
                <AudioOutlined />
              </div>
              <div className="prep-title">准备开始会议</div>
              <div className="prep-desc">
                开始会议后，小水华将实时记录会议内容，并在会议结束后自动生成纪要。
              </div>
              <div style={{ marginTop: 14 }}>
                <Button
                  type="primary"
                  size="large"
                  icon={<PlayCircleOutlined />}
                  loading={startLoading}
                  onClick={handleStart}
                >
                  开始会议
                </Button>
              </div>
            </div>
          </section>
        </div>
      </div>
    )
  }

  // ---- 其它状态：IN_PROGRESS / FINALIZING / COMPLETED ----
  // 四态动作严格：只有 IN_PROGRESS 显示「结束会议」；FINALIZING / COMPLETED 无 start/finish
  const headerAction = meeting.status === 'IN_PROGRESS' ? (
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
    ) : meeting.status === 'COMPLETED' ? (
      <Button
        type="primary"
        icon={<FileTextOutlined />}
        onClick={() => navigate(`/meetings/${meetingId}/result`)}
      >
        查看会议结果
      </Button>
    ) : undefined

  // section head 文案随状态变化（8B-5）。数据语义不变，仅展示文案。
  const isLive = meeting.status === 'IN_PROGRESS' || meeting.status === 'FINALIZING'
  const sectionTitle =
    meeting.status === 'FINALIZING'
      ? '正在整理会议'
      : meeting.status === 'COMPLETED'
        ? '会议记录'
        : '实时记录'
  const sectionHint =
    meeting.status === 'FINALIZING'
      ? '正在接收最后的转写内容，并准备生成 AI 纪要。'
      : meeting.status === 'COMPLETED'
        ? '转写已结束'
        : 'AI 实时转写 · 自动保存'

  return (
    <div className="page page--wide" style={{ minHeight: 0 }}>
      <Header
        meeting={meeting}
        action={headerAction}
        timer={meeting.status === 'IN_PROGRESS' ? durationText : undefined}
        onBack={() => navigate('/meetings')}
        details={headerDetails}
      />

      <div className="room__body room__body--active">
        <section className="room__transcript">
          <div className="room__section-head">
            {isLive ? <span className="live-dot" aria-hidden /> : null}
            <span>{sectionTitle}</span>
            {sectionHint ? <span className="room__section-hint">{sectionHint}</span> : null}
          </div>

          {/* FINALIZING：收尾三态指示（无 fake 百分比，均为真实阶段） */}
          {meeting.status === 'FINALIZING' ? (
            <div className="finish-steps">
              <div className="finish-step">
                <span className="finish-step__dot">
                  <CheckOutlined />
                </span>
                <span>会议已结束</span>
              </div>
              <div className="finish-step finish-step--active">
                <span className="finish-step__dot finish-step__dot--pulse" aria-hidden />
                <span>转写收尾中</span>
              </div>
              <div className="finish-step finish-step--pending">
                <span className="finish-step__dot finish-step__dot--ring" aria-hidden />
                <span>AI 纪要等待生成</span>
              </div>
            </div>
          ) : null}

          {/* UI-INTEL-2A：evidence 定位提示（只读、轻量，不阻塞转写）。
              加载中 → 「正在加载原始记录…」（Snapshot 到位后自动补做定位，不再发请求）；
              找不到 → 「原始记录暂不可定位」（不弹窗、不影响其它功能）。 */}
          {evidence.outcome !== 'idle' ? (
            <div className="transcript-note" role="status" aria-live="polite">
              {evidence.outcome === 'loading'
                ? '正在加载原始记录…'
                : '原始记录暂不可定位'}
            </div>
          ) : null}

          <TranscriptBody
            status={meeting.status}
            segments={finalSegments}
            partials={partialList}
            streamStatus={streamStatus}
            error={transcriptError}
            loading={transcriptLoading}
            onRetry={retryTranscript}
            listRef={segmentsRef}
          />

          {/* 8B-4 FAB：悬浮于转写面板右下，出现时轻升起；hover 轻抬、点击微压。
              UI-INTEL-2A：evidence 跳转后此按钮即「回到最新」的唯一恢复入口 ——
              文案不依赖「有新内容」，因为跳转本身也会让用户离开底部。 */}
          {showJumpButton ? (
            <Button
              className="transcript-jump"
              size="small"
              icon={<ArrowDownOutlined />}
              onClick={jumpToBottom}
            >
              回到最新
            </Button>
          ) : null}
        </section>

        <aside className="room__intelligence">
          <MeetingIntelligencePanel
            state={intelligence.state}
            loading={intelligence.loading}
            error={intelligence.error}
            isMeetingLive={meeting.status === 'IN_PROGRESS'}
            pendingSegmentCount={pendingSegmentCount}
            onRefresh={intelligence.refreshState}
            onTraceEvidence={handleTraceEvidence}
          />
        </aside>
      </div>
    </div>
  )
}
