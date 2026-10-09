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
import { CaptainPanel } from '@/components/CaptainPanel'
import { PixelCaptainDock } from '@/components/PixelCaptainDock'
import { DemoComposer } from '@/components/DemoComposer'
import { MeetingStatusTag } from '@/components/MeetingStatusTag'
import { useMeetingStore } from '@/stores/meetingStore'
import { useMeetingLoad } from '@/hooks/useMeetingLoad'
import { useMeetingIntelligence } from '@/hooks/useMeetingIntelligence'
import { useCaptainReferee } from '@/hooks/useCaptainReferee'
import { usePixelCaptainState } from '@/hooks/usePixelCaptainState'
import { useTranscriptSnapshot } from '@/hooks/useTranscriptSnapshot'
import { useMeetingStream } from '@/hooks/useMeetingStream'
import { useAutoScroll } from '@/hooks/useAutoScroll'
import { useTranscriptEvidenceNavigation } from '@/hooks/useTranscriptEvidenceNavigation'
import { durationBetweenMs, formatDuration, elapsedMs } from '@/utils/time'
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
  onBack,
  details,
}: {
  meeting: Meeting
  action?: React.ReactNode
  onBack?: () => void
  details?: React.ReactNode
}) {
  const title = meeting.title?.trim() || '未命名会议'
  return (
    <header className="room__header">
      <div className="room__header-toolbar">
        {onBack ? (
          <button type="button" className="room__back" onClick={onBack}>
            <ArrowLeftOutlined aria-hidden />
            返回会议列表
          </button>
        ) : <span />}
        {action ? <div className="room__header-action">{action}</div> : null}
      </div>
      <div className="room__identity-grid">
        <div className="room-identity room-identity--name">
          <span className="room-identity__label">会议名称</span>
          <strong className="room__title" title={meeting.title || undefined}>{title}</strong>
        </div>
        <div className="room-identity">
          <span className="room-identity__label">会议号</span>
          <strong className="room__id" title={meeting.meeting_code}>{meeting.meeting_code}</strong>
        </div>
        <div className="room-identity">
          <span className="room-identity__label">会议状态</span>
          <strong><MeetingStatusTag status={meeting.status} /></strong>
        </div>
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

/** 会议开始后的真实时间偏移；缺少 ASR 时间戳时不制造展示值。 */
function timelineTimeText(startMs: number | null): string {
  if (startMs === null) return ''
  const duration = formatDuration(startMs)
  return duration.startsWith('00:') ? duration.slice(3) : duration
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
 * FINAL 是紧凑时间线纯文本；PARTIAL 是唯一保留消息框的“正在输入”内容。
 * speaker_id 继续留在数据层，但不向用户展示不可靠的话者身份。
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
      <ol className="transcript-segments transcript-timeline" ref={listRef}>
        {segments.map((segment) => {
          const time = timelineTimeText(segment.start_ms)
          return (
            <li key={segment.segment_seq} className="transcript-timeline__item">
              <span className="transcript-timeline__dot" aria-hidden />
              <div className="transcript-timeline__content">
                {time ? (
                  <time className="transcript-timeline__time" aria-label={`会议开始后 ${time}`}>
                    {time}
                  </time>
                ) : null}
                <div
                  className="transcript-segment transcript-timeline__text"
                  /* segment_seq remains the sole evidence navigation key. */
                  data-segment-seq={segment.segment_seq}
                >
                  <span className="transcript-segment__text">{segment.text}</span>
                </div>
              </div>
            </li>
          )
        })}
        {partials.map((p) => (
          <li
            key={p.utterance_id}
            className="transcript-timeline__item transcript-timeline__item--current"
          >
            <span className="transcript-timeline__dot transcript-timeline__dot--current" aria-hidden />
            <div className="transcript-current">
              <div className="transcript-current__meta">
                <span className="transcript-current__live">
                  <AudioOutlined aria-hidden />
                  正在转写
                </span>
                <span className="transcript-typing" aria-hidden>
                  <i />
                  <i />
                  <i />
                </span>
              </div>
              <div className="transcript-segment transcript-segment--partial transcript-current__text">
                <span className="transcript-segment__text">{p.text}</span>
              </div>
            </div>
          </li>
        ))}
      </ol>
    </div>
  )
}

function TranscriptActivity({
  status,
  streamStatus,
}: {
  status: Meeting['status']
  streamStatus: ReturnType<typeof useMeetingStore.getState>['streamStatus']
}) {
  const connected = streamStatus === 'connected'
  const isActive = connected && (status === 'IN_PROGRESS' || status === 'FINALIZING')
  const isPending = streamStatus === 'connecting' || streamStatus === 'reconnecting'

  const primary = status === 'COMPLETED'
    ? '会议音频已结束'
    : status === 'FINALIZING'
      ? '正在接收最后的会议音频'
      : isPending
        ? '正在恢复音频连接'
        : connected
          ? '正在接收会议音频'
          : '等待会议音频'
  const secondary = status === 'COMPLETED'
    ? '转写已保存'
    : status === 'FINALIZING'
      ? '转写收尾中'
      : connected
        ? '实时转写中'
        : isPending
          ? '连接中'
          : '等待连接'

  return (
    <div
      className={`transcript-activity${isActive ? ' transcript-activity--active' : ''}${isPending ? ' transcript-activity--pending' : ''}`}
      role="status"
      aria-live="polite"
    >
      <span className="transcript-activity__label">
        <span className="transcript-activity__dot" aria-hidden />
        {primary}
      </span>
      <span className="transcript-activity__wave" aria-hidden>
        {Array.from({ length: 32 }, (_, index) => <i key={index} />)}
      </span>
      <span className="transcript-activity__state">{secondary}</span>
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
        meeting.ended_at
          ? durationBetweenMs(meeting.started_at, meeting.ended_at)
          : elapsedMs(meeting.started_at, new Date(now)),
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

  // ADR-002 前端接入：水华队长裁判面板。同样是独立数据域 —— 数据留在 hook 内，
  // 失败只降级本面板，绝不影响转写 / 生命周期 / Meeting Intelligence。
  // enabled 条件与 Meeting State 略有不同：FINALIZING 仍可请求裁判（PRD 允许），
  // 而 COMPLETED 属归档态、不再新建报告。
  const canTriggerCaptain =
    meeting?.status === 'IN_PROGRESS' || meeting?.status === 'FINALIZING'
  const captain = useCaptainReferee(
    meetingId,
    !!meeting && meeting.status !== 'CREATED',
  )

  // Phase 3-1：水华队长像素角色。**不是第二个数据源** —— 它的状态完全由上面
  // 那几个真实字段派生（见 usePixelCaptainState 的状态来源对照表）。
  // 没有计时器，没有乐观完成：COMPLETED 只在服务端真的落库了报告之后出现。
  // 展开状态也留在 hook 内，MeetingRoomPage 只读一个 view 对象。
  const pixelCaptain = usePixelCaptainState(
    {
      report: captain.report,
      loading: captain.loading,
      triggering: captain.triggering,
      error: captain.error,
    },
    canTriggerCaptain,
  )

  // 8B-4：SSE 流（含 Snapshot-first 连接、断线恢复、meeting.status→GET Meeting）
  // UI-INTEL-1：**复用同一条 /events 连接**承接 meeting.state.updated 失效通知，
  // 收到后 refreshState() 重新 GET /state —— 不新增第二条 EventSource。
  // ADR-002 Phase 2-A：同一条连接也承接 captain.completed / captain.failed，
  // 收到后按 report_id 回源读那一份报告（同样不消费 payload）。
  // Task 6：captain.quick_assessment 是同一条连接上的第三种裁判事件 ——
  // 它是两阶段裁判的第一段，**不回源**（报告还没落盘），直接交给 captain
  // 面板渲染临时卡片，同时**不停止** loading。
  useMeetingStream({
    meetingId,
    status: meeting?.status,
    snapshotReady,
    maxSegmentSeq,
    onStateInvalidated: intelligence.refreshState,
    // Phase C.5：两个裁判事件共用这**一个**回调，而不是分别接 invalidate /
    // acceptQuick。原因是归属规则必须由**同一个** owner 持有：如果
    // `captain.completed(A)` 和 `captain.quick_assessment(B)` 分别进入两个
    // 独立回调，接受 B 的临时结果时就没有任何东西知道「A 已经是终态」——
    // 那份临时卡片会挂到已经落盘的 A 上。合并成一个入口后，hook 内部持有
    // reportId / status 两个 ref，两条规则在一处判定。
    onCaptainEvent: captain.handleCaptainEvent,
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
        <Button
          className="room__primary-action room__primary-action--danger"
          type="primary"
          danger
          icon={<StopOutlined />}
          loading={finishLoading}
        >
          结束会议
        </Button>
      </Popconfirm>
    ) : meeting.status === 'COMPLETED' ? (
      <Button
        className="room__primary-action"
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
    <div className="page page--wide room-page--workspace">
      <Header
        meeting={meeting}
        action={headerAction}
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

          <TranscriptActivity status={meeting.status} streamStatus={streamStatus} />

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
          {/* Phase 3-1：水华队长像素角色常驻在右轨顶部。它平时只是一个安静的
              小角色（IDLE），不占面板高度；触发后表演，COMPLETED 时给出展开
              开关。它不遮挡任何内容 —— 角色与气泡全部在文档流内，没有
              absolute / fixed，所以既不会盖住左边的转写，也不会盖住底部的
              演示输入条（对照：.transcript-jump 是 absolute，但它被转写面板
              自己的 stacking context 关在里面）。 */}
          {isLive ? (
            <PixelCaptainDock
              phase={pixelCaptain.phase}
              caption={pixelCaptain.caption}
              canTrigger={canTriggerCaptain}
              hasReport={pixelCaptain.hasReport}
              expanded={pixelCaptain.expanded}
              onToggle={pixelCaptain.toggle}
              // IDLE / FAILED 两个阶段的轻量入口。**与演示输入条的快捷发送
              // 是两条真实路径**：快捷发送走唤醒词 ingress（一句话落库 + 服务端
              // 判定唤醒），这里走 POST /captain（服务端取当前高水位）。
              // 两条都合法、都幂等，服务端各自负责锚定；前端不伪造 anchor。
              onTrigger={() => void captain.trigger()}
              triggering={captain.triggering}
              report={captain.report}
            />
          ) : null}

          {/* ADR-002：裁判报告就绪 / 分析中 / 真的出错时才「跳出来」。
              会议从未被裁判过（error === 'no_report'）是**正常状态**，不是
              值得占一屏的空态 —— 那时右轨留给 AI 会议理解，裁判通过演示输入
              的快捷按钮或直接说出唤醒词唤起。

              Phase 3-1：**只有像素角色把报告展开时**才渲染这个面板。
              其数据展示能力原样保留（同一个组件、同一批 props），但不再
              无条件占据右轨 —— 两个入口指向同一份 COMPLETED 报告，
              不会出现两份。角色自身（IDLE/TRIGGERED/ANALYZING/FAILED）
              已经承担了「还没有报告」时的全部说明。 */}
          {pixelCaptain.expanded ? (
            <CaptainPanel
              report={captain.report}
              quick={captain.quick}
              loading={captain.loading}
              triggering={captain.triggering}
              error={captain.error}
              canTrigger={canTriggerCaptain}
              onTrigger={() => void captain.trigger()}
              onRefresh={() => captain.invalidate()}
              onTraceEvidence={handleTraceEvidence}
            />
          ) : null}

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

      {/* 演示输入：会议进行中常驻于页面底部。它提交的每一句都会成为真实
          持久化 FINAL（走与 ESP32 相同的 ingress），所以这里看到的是真实链路，
          不是模拟数据。会议结束后不再接受发言，故不渲染。 */}
      {isLive ? (
        <DemoComposer
          meetingId={meeting.id}
          disabled={false}
          onSent={() => jumpToBottom()}
        />
      ) : null}
    </div>
  )
}
