import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Button, Card, Popconfirm, Skeleton, Tabs, Typography, message } from 'antd'
import {
  ArrowLeftOutlined,
  ReloadOutlined,
  FileTextOutlined,
} from '@ant-design/icons'
import { EmptyPlaceholder } from '@/components/EmptyPlaceholder'
import { MeetingStatusTag } from '@/components/MeetingStatusTag'
import { regenerateSummary, normalizeApiError } from '@/api'
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
 * 8C-3：Regenerate（重新生成纪要）。
 *   - 入口在「会议纪要」区标题行右侧（secondary button + Popconfirm）。
 *   - POST /summary/regenerate（202）→ Backend append-only 分配 vN+1 →
 *     立刻 GET /summary 拿到 GENERATING → streamTerminal 转 false → SSE 自动重建。
 *   - **不猜 version、不改 Meeting lifecycle**（全程 COMPLETED）。
 *   - 生成/失败期间保留并继续展示上一次可用纪要（Banner 提示），不清空页面。
 *
 * 本 Step 明确不做：版本切换 / 版本历史 / 版本对比（8C-3 只展示 latest）。
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

  // ---- 8C-2/8C-3 Summary 实时通知 ----
  // 连接条件（spec：仅 summary 非 terminal 时连 SSE）：
  //   - meeting FINALIZING：纪要生成中 / 即将生成 → 连；
  //   - meeting COMPLETED + summary GENERATING：**重新生成中（8C-3）** → 连；
  //   - 其它（COMPLETED 且 summary 终态、CREATED/IN_PROGRESS、meeting 未加载）→ 不连。
  // meeting 未加载前（null）不连，避免在未知状态上挂流。
  const streamTerminal = !(
    meeting?.status === 'FINALIZING' ||
    (meeting?.status === 'COMPLETED' && summary?.status === 'GENERATING')
  )
  // onSummaryEvent：SSE 只是通知通道，只触发 GET（Server 是事实源），
  // 绝不把 SSE 的 {version,status} payload 直接当 Summary 渲染。
  //
  // 注意（8C-3）：这里**不能**用「summary 已 terminal 就跳过」做守卫 ——
  // 重新生成时 v1 是 COMPLETED（terminal），但 v2 正在生成，守卫会让页面
  // 永远停在 v1、SSE 也永不重连。真正的门控是 SSE 连接本身：terminal 时不连
  // 流，也就收不到事件；能收到事件就说明有生成在飞行中，应当重取。
  const handleSummaryEvent = useCallback(() => {
    if (!meetingId) return
    // GET /summary（useMeetingSummary）→ 成功后持久化快照覆盖 UI；
    // 404 → not_found → 渲染「正在生成」；重复 GET 幂等无害。
    retrySummary()
  }, [meetingId, retrySummary])

  useSummaryResultStream({
    meetingId,
    terminal: streamTerminal,
    onSummaryEvent: handleSummaryEvent,
  })

  // ---- 8C-3 Regenerate ----
  // 两种 loading 严格区分（spec §7）：
  //   submitting = POST 请求在飞行中（按钮 loading「正在提交…」）
  //   generating = Backend latest = GENERATING（LLM 在跑，按钮禁用 + Banner）
  const [submitting, setSubmitting] = useState(false)
  // generating：Backend latest 正在生成（LLM 在跑）——用于禁用 regenerate 入口。
  const generating = summary?.status === 'GENERATING'
  // 保留「上一次已知可渲染内容」：重新生成期间 latest 是 v2 GENERATING
  // （overview=null），若直接渲染会清空旧纪要。这里缓存最近一次 COMPLETED
  // 快照，生成/失败期间继续展示，避免整页消失（spec §8/§12）。
  const [lastGood, setLastGood] = useState<MeetingSummary | null>(null)
  useEffect(() => {
    if (summary?.status === 'COMPLETED') setLastGood(summary)
  }, [summary])

  const handleRegenerate = useCallback(async () => {
    if (!meetingId || submitting) return
    setSubmitting(true)
    try {
      // 不猜 version、不改 meeting：Backend 同步分配 vN+1（append-only）。
      await regenerateSummary(meetingId)
      // 立刻 GET /summary 同步真实状态 —— 拿到 v2 GENERATING 后，
      // streamTerminal 变 false → useSummaryResultStream 自动重建 SSE。
      retrySummary()
    } catch (raw: unknown) {
      const err = normalizeApiError(raw)
      if (err.kind === 'http' && err.status === 409) {
        // latest 已 GENERATING：不重复触发，只同步真实状态让 SSE 接管
        message.info('纪要正在生成中，请稍候。')
        retrySummary()
      } else {
        // 网络/其它失败：只表示「请求失败」，绝不改 Summary 状态（§14）
        message.error(err.message || '重新生成请求失败')
      }
    } finally {
      setSubmitting(false)
    }
  }, [meetingId, submitting, retrySummary])

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

  // ---- 8C-3：regenerate 入口的显示条件（严格按 actual contract §21）----
  // Backend 允许 regenerate 当且仅当 meeting ∈ {FINALIZING, COMPLETED} 且
  // latest summary 不是 GENERATING。另外 meeting 无 summary 时不显示 ——
  // regenerate 是「重新生成」，首次生成由 Finalizer 自动触发，不用它代替（§20）。
  const canRegenerate =
    !submitting &&
    !generating &&
    summary !== null &&
    (meeting.status === 'COMPLETED' || meeting.status === 'FINALIZING')

  /** 纪要区标题行：左标题（可含版本），右「重新生成纪要」入口 */
  const summaryHead = (versionText?: string) => (
    <div className="summary-head">
      <div className="summary-head__title">
        AI 纪要
        {versionText ? <span className="summary-head__version">{versionText}</span> : null}
      </div>
      {summary !== null || lastGood !== null ? (
        <Popconfirm
          title="重新生成纪要"
          description="重新生成会创建一个新的纪要版本，当前纪要不会被删除。"
          okText="重新生成"
          cancelText="取消"
          onConfirm={handleRegenerate}
          disabled={!canRegenerate}
        >
          <Button size="small" loading={submitting} disabled={!canRegenerate}>
            {submitting ? '正在提交…' : '重新生成纪要'}
          </Button>
        </Popconfirm>
      ) : null}
    </div>
  )

  /** 生成中 / 新版失败：轻量 Banner（旧内容仍展示在其下方） */
  const summaryBanner = (() => {
    if (summary?.status === 'GENERATING') {
      // version > 1 → 重新生成（旧版仍可读）；v1 → 首次生成
      const isNewVersion = summary.version > 1
      return (
        <div className="summary-banner summary-banner--live">
          <span className="summary-banner__dot" aria-hidden />
          <div className="summary-banner__body">
            <div className="summary-banner__title">
              {isNewVersion ? '正在生成新版纪要' : 'AI 纪要正在生成'}
            </div>
            <div className="summary-banner__hint">
              {isNewVersion
                ? '当前纪要仍可查看，生成完成后将自动更新。'
                : '会议内容已经保存，纪要完成后即可查看。'}
            </div>
          </div>
        </div>
      )
    }
    if (summary?.status === 'FAILED' && lastGood) {
      return (
        <div className="summary-banner summary-banner--warn">
          <div className="summary-banner__body">
            <div className="summary-banner__title">新版纪要生成失败</div>
            <div className="summary-banner__hint">下方仍为上一个可用版本的纪要内容。</div>
          </div>
        </div>
      )
    }
    return null
  })()

  const summaryTabContent = (() => {
    // loading：仅在没有可展示内容时给 skeleton（有旧版则保留旧版，不闪空）
    if (summaryLoading && !summary && !lastGood) {
      return <Skeleton active paragraph={{ rows: 4 }} title={false} />
    }
    // 网络失败：独立重试，不影响其它域
    if (summaryError && summaryError !== 'not_found' && !summary && !lastGood) {
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
    // 待生成：meeting FINALIZING 且尚无任何版本 —— 首次生成（非 regenerate）
    if (!summary && !lastGood) {
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
    // 有可渲染内容：COMPLETED 用当前版本；GENERATING/FAILED 退回上一次可用版本
    // （F5 期间 lastGood 可能为空 —— 此时 summary 自身就是 GENERATING/FAILED）
    const display = summary?.status === 'COMPLETED' ? summary : lastGood
    if (display) {
      return (
        <div className="summary-wrap">
          {summaryHead(`v${display.version}`)}
          {summaryBanner}
          <SummarySections summary={display} />
        </div>
      )
    }
    // 无可用旧内容：
    //  - 当前 GENERATING（如 F5 后 v2 正在生成，旧版未在本会话加载过）→ 只给 Banner
    if (summary?.status === 'GENERATING') {
      return (
        <div className="summary-wrap">
          {summaryHead(`v${summary.version}`)}
          {summaryBanner}
        </div>
      )
    }
    //  - 首版就 FAILED（无旧版可退）→ 失败态 + 重新生成入口
    return (
      <div className="summary-wrap">
        {summaryHead()}
        <EmptyPlaceholder
          icon={<ReloadOutlined />}
          title="AI 纪要生成失败"
          description="该会议纪要生成时出现问题，可尝试重新生成。"
          action={
            canRegenerate ? (
              <Popconfirm
                title="重新生成纪要"
                description="重新生成会创建一个新的纪要版本，当前纪要不会被删除。"
                okText="重新生成"
                cancelText="取消"
                onConfirm={handleRegenerate}
              >
                <Button size="small" loading={submitting} type="primary">
                  {submitting ? '正在提交…' : '重新生成纪要'}
                </Button>
              </Popconfirm>
            ) : undefined
          }
          compact
        />
      </div>
    )
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
            <li
              key={seg.segment_seq}
              className="transcript-segment"
              /* 与 MeetingRoom 用同一个稳定标识，为 UI-INTEL-2B（会后证据溯源）
                 预留；本步 Result 页不接线 evidence 交互。 */
              data-segment-seq={seg.segment_seq}
            >
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
