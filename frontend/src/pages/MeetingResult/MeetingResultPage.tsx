import { useCallback } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Button, Card, Skeleton, Tabs, Typography } from 'antd'
import {
  ArrowLeftOutlined,
  ReloadOutlined,
  FileTextOutlined,
} from '@ant-design/icons'
import { EmptyPlaceholder } from '@/components/EmptyPlaceholder'
import { MeetingStatusTag } from '@/components/MeetingStatusTag'
import { useMeetingLoad } from '@/hooks/useMeetingLoad'
import { useMeetingSummary } from '@/hooks/useMeetingSummary'
import { useSummaryResultStream } from '@/hooks/useSummaryResultStream'
import { useTranscriptSnapshotResult } from '@/hooks/useTranscriptSnapshotResult'
import { useMeetingStore } from '@/stores/meetingStore'
import { formatDateTime, formatDuration } from '@/utils/time'
import type { Meeting } from '@/types/meeting'
import type { MeetingSummary } from '@/types/summary'

const { Title } = Typography

/** 时长（已结束会议：started_at -> ended_at/now） */
function resultDurationText(meeting: Meeting): string {
  if (meeting.started_at && meeting.ended_at) {
    return formatDuration(Math.max(0, new Date(meeting.ended_at).getTime() - new Date(meeting.started_at).getTime()))
  }
  if (meeting.status === 'IN_PROGRESS' && meeting.started_at) {
    return formatDuration(Date.now() - new Date(meeting.started_at).getTime())
  }
  return ''
}

/**
 * Summary 分节渲染 —— 只展示后端真实字段（SummaryOut），无 fake。
 * overview / key_topics / conclusions / open_questions 均来自 actual schema。
 * 空数组不显示大空卡片（弱化）。
 */
function SummarySections({ summary }: { summary: MeetingSummary }) {
  const blocks: { label: string; body: React.ReactNode }[] = []
  if (summary.overview?.trim()) {
    blocks.push({
      label: '会议概述',
      body: <p className="summary__placeholder">{summary.overview}</p>,
    })
  }
  if (summary.key_topics.length > 0) {
    blocks.push({
      label: '核心议题',
      body: (
        <ol className="summary-list">
          {summary.key_topics.map((t, i) => (
            <li key={i}>{t}</li>
          ))}
        </ol>
      ),
    })
  }
  if (summary.conclusions.length > 0) {
    blocks.push({
      label: '主要结论',
      body: (
        <ol className="summary-list">
          {summary.conclusions.map((c, i) => (
            <li key={i}>{c}</li>
          ))}
        </ol>
      ),
    })
  }
  if (summary.open_questions.length > 0) {
    blocks.push({
      label: '未决问题',
      body: (
        <ol className="summary-list">
          {summary.open_questions.map((q, i) => (
            <li key={i}>{q}</li>
          ))}
        </ol>
      ),
    })
  }

  if (blocks.length === 0) {
    return (
      <EmptyPlaceholder
        icon={<FileTextOutlined />}
        title="暂无纪要内容"
        description="AI 纪要内容为空。"
        compact
      />
    )
  }

  return (
    <div className="summary">
      {blocks.map((b) => (
        <div className="summary__block" key={b.label}>
          <div className="summary__label">{b.label}</div>
          {b.body}
        </div>
      ))}
      {summary.version ? (
        <div className="summary__version">版本 v{summary.version}</div>
      ) : null}
    </div>
  )
}

/**
 * Meeting Result —— Step 8C-2：真实 Summary + Transcript 展示 + Summary 实时通知。
 *
 * 恢复模型（刷新后从 Backend 恢复，不依赖 Zustand 残留）：
 *   GET Meeting → GET Summary Snapshot（latest version）→ GET Transcript Snapshot
 * 三个数据域独立：
 *   - Meeting  域（useMeetingLoad，404 / network）—— Header + 页面存在性
 *   - Summary  域（useMeetingSummary，独立 retry）—— 会议纪要 Tab
 *   - Transcript域（useTranscriptSnapshotResult，独立 retry）—— 会议内容 Tab
 * 任一域失败不拖垮其它域。
 *
 * 8C-2：Summary 实时通知（useSummaryResultStream）。
 *   - SSE 只是通知通道，GET /summary 是 durable 事实源：
 *     summary.started / completed / failed 到达 → 只重新 GET /summary（或 GET /meeting）；
 *     **绝不**把 SSE 的 {version,status} payload 直接当 Summary 渲染。
 *   - 仅当 summary 非 terminal（meeting FINALIZING / summary missing / GENERATING）
 *     时连接 SSE；terminal 后关闭；手动「刷新状态」仍是兜底 fallback。
 *   - Result 是归档阅读态：不消费 transcript.partial / final，不连 Transcript SSE。
 *
 * 本 Step 明确不做：Regenerate / 版本切换（留 8C-3）。
 */
export function MeetingResultPage() {
  const { meetingId } = useParams<{ meetingId: string }>()
  const navigate = useNavigate()

  // Meeting 域（复用 MeetingRoom 的 load 模式；404 / network 轻量态）
  const meeting = useMeetingStore((s) => s.meeting)
  const meetingLoading = useMeetingStore((s) => s.loading)
  const meetingError = useMeetingStore((s) => s.error)
  useMeetingLoad(meetingId)

  // Summary 域（latest version）
  const { summary, loading: summaryLoading, error: summaryError, retry: retrySummary } =
    useMeetingSummary(meetingId)

  // Transcript 域（持久化 FINAL only，ASC）
  const { segments, loading: transcriptLoading, error: transcriptError, retry: retryTranscript } =
    useTranscriptSnapshotResult(meetingId)

  // ---- 8C-2 Summary 实时通知 ----
  // 连接条件（spec：仅 summary 非 terminal 时连 SSE）：
  //   - meeting FINALIZING：纪要生成中 / 即将生成 → 连；
  //   - meeting COMPLETED + summary GENERATING：重新生成中（8C-3 预留，天然兼容）→ 连；
  //   - 其它（COMPLETED 且 summary 终态、CREATED/IN_PROGRESS、meeting 未加载）→ 不连。
  // meeting 未加载前（null）不连，避免在未知状态上挂流。
  const summaryTerminal =
    summary?.status === 'COMPLETED' || summary?.status === 'FAILED'
  const streamTerminal = !(
    meeting?.status === 'FINALIZING' ||
    (meeting?.status === 'COMPLETED' && summary?.status === 'GENERATING')
  )
  // onSummaryEvent：SSE 只是通知通道，只触发 GET（Server 是事实源），
  // 绝不把 SSE 的 {version,status} payload 直接当 Summary 渲染。
  const handleSummaryEvent = useCallback(() => {
    if (summaryTerminal || !meetingId) return
    // GET /summary（useMeetingSummary）→ 成功后持久化快照覆盖 UI；
    // 404 → not_found → 渲染「正在生成」；meeting.status(COMPLETED) 时
    // 重复 GET 无害（already COMPLETED → 快照即最终态）。
    retrySummary()
  }, [summaryTerminal, meetingId, retrySummary])

  useSummaryResultStream({
    meetingId,
    terminal: streamTerminal,
    onSummaryEvent: handleSummaryEvent,
  })

  const handleBack = () => {
    if (window.history.length > 1) {
      navigate(-1)
    } else {
      navigate('/meetings')
    }
  }

  // ---- Meeting 404 / network：页面级轻量态 ----
  if (meetingError === 'not_found') {
    return (
      <div className="page page--wide" style={{ minHeight: 0 }}>
        <div className="panel" style={{ padding: 32 }}>
          <EmptyPlaceholder
            icon={<FileTextOutlined />}
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
  if (meetingError && meetingError !== 'not_found') {
    return (
      <div className="page page--wide" style={{ minHeight: 0 }}>
        <div className="panel" style={{ padding: 32 }}>
          <EmptyPlaceholder
            icon={<ReloadOutlined />}
            title="会议加载失败"
            description={meetingError === 'network'
              ? '无法连接后端服务，请确认服务已启动后重试。'
              : meetingError}
            action={
              <Button type="primary" icon={<ReloadOutlined />} onClick={() => window.location.reload()}>
                重试
              </Button>
            }
          />
        </div>
      </div>
    )
  }

  // ---- Header skeleton（meeting 未到） ----
  if (meetingLoading || !meeting) {
    return (
      <div className="page page--wide" style={{ minHeight: 0 }}>
        <Skeleton active paragraph={{ rows: 1 }} title={{ width: 240 }} />
        <div style={{ marginTop: 20 }}>
          <Card style={{ borderRadius: 16, borderColor: '#E8EEF5' }}>
            <Skeleton active paragraph={{ rows: 6 }} />
          </Card>
        </div>
      </div>
    )
  }

  const durationText = resultDurationText(meeting)

  const summaryTabContent = (() => {
    // loading：section skeleton
    if (summaryLoading) {
      return <Skeleton active paragraph={{ rows: 4 }} title={false} />
    }
    // 网络失败：独立重试，不影响其它域
    if (summaryError && summaryError !== 'not_found') {
      return (
        <EmptyPlaceholder
          icon={<ReloadOutlined />}
          title="纪要加载失败"
          description={summaryError === 'network'
            ? '无法连接后端服务，请确认服务已启动后重试。'
            : summaryError}
          action={
            <Button size="small" icon={<ReloadOutlined />} onClick={retrySummary}>
              重新加载
            </Button>
          }
          compact
        />
      )
    }
    // meeting 存在但 summary 未生成
    if (summaryError === 'not_found' || !summary) {
      return (
        <EmptyPlaceholder
          icon={<FileTextOutlined />}
          title={meeting.status === 'FINALIZING' ? 'AI 纪要正在生成' : '纪要尚未生成'}
          description={meeting.status === 'FINALIZING'
            ? '会议内容已经保存，纪要完成后即可查看。'
            : '会议结束后将自动生成 AI 纪要。'}
          action={
            meeting.status === 'FINALIZING' ? (
              <Button size="small" icon={<ReloadOutlined />} onClick={retrySummary}>
                刷新状态
              </Button>
            ) : undefined
          }
          compact
        />
      )
    }
    // Summary 存在
    if (summary.status === 'FAILED') {
      return (
        <EmptyPlaceholder
          icon={<ReloadOutlined />}
          title="AI 纪要生成失败"
          description="该会议纪要生成时出现问题，稍后可重新生成。"
          compact
        />
      )
    }
    if (summary.status === 'GENERATING' || summary.status === 'NOT_STARTED') {
      return (
        <EmptyPlaceholder
          icon={<FileTextOutlined />}
          title="AI 纪要正在生成"
          description="会议内容已经保存，纪要完成后即可查看。"
          action={
            <Button size="small" icon={<ReloadOutlined />} onClick={retrySummary}>
              刷新状态
            </Button>
          }
          compact
        />
      )
    }
    // COMPLETED：真实分节
    return <SummarySections summary={summary} />
  })()

  const transcriptTabContent = (() => {
    if (transcriptLoading) {
      return <Skeleton active paragraph={{ rows: 5 }} title={false} />
    }
    if (transcriptError) {
      return (
        <EmptyPlaceholder
          icon={<ReloadOutlined />}
          title="会议内容加载失败"
          description={transcriptError === 'network'
            ? '无法连接后端服务，请确认服务已启动后重试。'
            : transcriptError}
          action={
            <Button size="small" icon={<ReloadOutlined />} onClick={retryTranscript}>
              重新加载
            </Button>
          }
          compact
        />
      )
    }
    if (segments.length === 0) {
      return (
        <EmptyPlaceholder
          icon={<FileTextOutlined />}
          title="暂无会议转写内容"
          description="本次会议未流转写出已确认的 FINAL 内容。"
          compact
        />
      )
    }
    return (
      <div className="transcript-surface transcript-surface--top">
        <ol className="transcript-segments">
          {segments.map((seg) => (
            <li key={seg.segment_seq} className="transcript-segment">
              <span className="transcript-segment__speaker">{seg.speaker_id}</span>
              <span className="transcript-segment__text">{seg.text}</span>
            </li>
          ))}
        </ol>
      </div>
    )
  })()

  return (
    <div className="page">
      <div className="result-head">
        <button type="button" className="room__back" onClick={handleBack}>
          <ArrowLeftOutlined aria-hidden />
          返回
        </button>
        <Title level={1} className="result-head__title h-m0">
          {meeting.title?.trim() || '未命名会议'}
        </Title>
        <div className="result-head__meta">
          <MeetingStatusTag status={meeting.status} />
          {meeting.started_at ? (
            <>
              <span aria-hidden>·</span>
              <span>{formatDateTime(meeting.started_at)}</span>
            </>
          ) : null}
          {durationText ? (
            <>
              <span aria-hidden>·</span>
              <span className="u-tabular">{durationText}</span>
            </>
          ) : null}
          {meeting.id ? (
            <>
              <span aria-hidden>#</span>
              <span className="result-head__id">{meeting.id}</span>
            </>
          ) : null}
        </div>
      </div>

      <Card
        className="result-tabs"
        style={{ borderRadius: 16, borderColor: '#E8EEF5' }}
        styles={{ body: { padding: '4px 24px 24px' } }}
      >
        <Tabs
          defaultActiveKey="summary"
          items={[
            {
              key: 'summary',
              label: '会议纪要',
              children: summaryTabContent,
            },
            {
              key: 'transcript',
              label: '会议内容',
              children: transcriptTabContent,
            },
          ]}
        />
      </Card>
    </div>
  )
}
