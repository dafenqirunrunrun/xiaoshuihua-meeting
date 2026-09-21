import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Button, Card, Popconfirm, Skeleton, Tabs, Typography, message } from 'antd'
import {
  ArrowLeftOutlined,
  ReloadOutlined,
  FileTextOutlined,
} from '@ant-design/icons'
import { EmptyPlaceholder } from '@/components/EmptyPlaceholder'
import { EvidenceTraceButton } from '@/components/EvidenceTraceButton'
import { MeetingStatusTag } from '@/components/MeetingStatusTag'
import { regenerateSummary, normalizeApiError } from '@/api'
import { useMeetingLoad } from '@/hooks/useMeetingLoad'
import { useMeetingSummary } from '@/hooks/useMeetingSummary'
import { useSummaryResultStream } from '@/hooks/useSummaryResultStream'
import { useTranscriptEvidenceNavigation } from '@/hooks/useTranscriptEvidenceNavigation'
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
 * 一个 action item 的展示文本。
 *
 * 必须与后端 `final_summary_prompts.to_legacy_schema` 的折叠格式**逐字一致**
 * （`task（owner，deadline）`，缺项就省略该段）。不一致不会报错，只会让
 * 「查看原文」入口消失 —— 这正是为什么「有 structured evidence 时走结构化渲染」
 * （见下）而不是靠比对扁平字符串去对齐。
 */
function formatActionItem(
  task: string,
  owner: string | null,
  deadline: string | null,
): string {
  const bits = [owner, deadline].filter((b): b is string => Boolean(b))
  return bits.length > 0 ? `${task}（${bits.join('，')}）` : task
}

/**
 * Summary 分节渲染 —— 只展示后端真实字段（SummaryOut），无 fake。
 * overview / key_topics / conclusions / open_questions 均来自 actual schema。
 * 空数组不显示大空卡片（弱化）。
 *
 * UI-INTEL-2B：V2 按核心议题 / 决策 / 待办 / 未决问题 / 风险分别展示，
 * 每个结构化条目都可带「查看原文」入口。
 *
 * **两条渲染路径，按 evidence 有无切换**：
 *   - `summary_evidence` 非 null（可验证 V2）→ 直接按五类**结构化字段**渲染，
 *     不再把待办混进结论、把风险混进未决问题。
 *   - `summary_evidence` 为 null（legacy / fallback / FAILED / 空会议）→
 *     原样渲染扁平字符串，**不出现任何入口**（§3：不猜、不反推、关键词匹配）。
 * 这样两边的展示在各自路径上都来自真实数据，且不需要一个「按位置猜对齐」的
 * 脆弱映射 —— 那种映射一旦格式漂移就会静默地把按钮变没。
 */
function SummarySections({
  summary,
  onTraceEvidence,
}: {
  summary: MeetingSummary
  onTraceEvidence: (seqs: number[]) => void
}) {
  const blocks: { label: string; body: React.ReactNode }[] = []
  if (summary.overview?.trim()) {
    blocks.push({
      label: '会议概述',
      body: <p className="summary__placeholder">{summary.overview}</p>,
    })
  }
  const ev = summary.summary_evidence

  // 一行 = 文本 + 可选 evidence 入口。seqs 为空时 EvidenceTraceButton 自己
  // 返回 null（§6 无 disabled 假按钮），这里不需要再判断一次。
  const tracedRow = (text: string, seqs: number[] | null, key: number) => (
    <li key={key} className="summary-list__item">
      <span>{text}</span>
      {seqs ? <EvidenceTraceButton sourceSegmentSeqs={seqs} onTrace={onTraceEvidence} /> : null}
    </li>
  )

  if (ev) {
    // --- V2 结构化渲染：五类信息保持各自语义，不再压平混排 ---
    if (ev.topics.length > 0) {
      blocks.push({
        label: '核心议题',
        body: (
          <ol className="summary-list">
            {ev.topics.map((t, i) =>
              tracedRow(`${t.title}：${t.summary}`, t.source_segment_seqs, i),
            )}
          </ol>
        ),
      })
    }
    if (ev.decisions.length > 0) {
      blocks.push({
        label: '已确认决策',
        body: (
          <ol className="summary-list">
            {ev.decisions.map((d, i) => tracedRow(d.text, d.source_segment_seqs, i))}
          </ol>
        ),
      })
    }
    if (ev.action_items.length > 0) {
      blocks.push({
        label: '待办事项',
        body: (
          <ol className="summary-list">
            {ev.action_items.map((a, i) =>
              tracedRow(
                formatActionItem(a.task, a.owner, a.deadline),
                a.source_segment_seqs,
                i,
              ),
            )}
          </ol>
        ),
      })
    }
    if (ev.open_questions.length > 0) {
      blocks.push({
        label: '未决问题',
        body: (
          <ol className="summary-list">
            {ev.open_questions.map((q, i) => tracedRow(q.text, q.source_segment_seqs, i))}
          </ol>
        ),
      })
    }
    if (ev.risks.length > 0) {
      blocks.push({
        label: '风险 / 顾虑',
        body: (
          <ol className="summary-list">
            {ev.risks.map((r, i) => tracedRow(r.text, r.source_segment_seqs, i))}
          </ol>
        ),
      })
    }
  } else {
    // --- 扁平渲染（legacy / fallback）：与 UI-INTEL-2B 之前完全一致，无入口 ---
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
 *
 * UI-INTEL-2B：Final Summary → Transcript Evidence Trace（会后证据溯源）。
 *   - 纪要条目（decision / action item / risk / open question）→「查看原文」→
 *     **切到「会议内容」Tab** → 等 Transcript DOM ready → 定位 + 高亮。
 *   - **不新增路由 query、不跳回 MeetingRoom**：Result 页本来就有这两个 Tab，
 *     切 Tab 是页面内行为。
 *   - 复用 UI-INTEL-2A 的 `useTranscriptEvidenceNavigation`（同一个 hook，
 *     同一套 data-segment-seq / 高亮 / 降级规则），**没有第二套 DOM 查询**。
 *   - **不引入 auto-follow**：会后 Transcript 是静态的，没有新内容来抢滚动，
 *     所以这里不调用 `pauseAutoFollow` / `jumpToBottom`（§7）。协作是可选的，
 *     hook 本身与 auto-follow 无耦合。
 */
export function MeetingResultPage() {
  const { meetingId } = useParams<{ meetingId: string }>()
  const navigate = useNavigate()
  const [activeTab, setActiveTab] = useState('summary')
  const transcriptRef = useRef<HTMLDivElement | null>(null)

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

  // ---- UI-INTEL-2B：会后证据溯源 -------------------------------------------
  // 复用 UI-INTEL-2A 的 hook：同一套 data-segment-seq 定位、同一套「滚动到
  // 最小有效 seq + 全部有效项同时高亮 + 全部缺失才提示」规则、同一个
  // `.transcript-segment--evidence` 样式。这里没有第二份 querySelector。
  //
  // 与 MeetingRoom 的唯一区别：**不接 auto-follow**（§7）。会后转写是静态
  // 归档，不会有新 FINAL 来抢滚动，因此不需要 pauseAutoFollow / jumpToBottom。
  // 该协作本来就是调用方的事，hook 与它无耦合。
  const evidence = useTranscriptEvidenceNavigation(transcriptRef, !transcriptLoading)

  /**
   * 「查看原文」：切到会议内容 Tab，**等容器真的挂载**再定位。
   *
   * 为什么不能就地调 `traceEvidence`（这是本页最容易写错的一处）：
   * `setActiveTab` 只是改我们自己的 state。AntD Tabs 用它**内部**的
   * `activeKey` 决定渲染哪个面板，并在后续一次 commit 才同步 —— 也就是说
   * `activeTab === 'transcript'` 已经被 React 提交时，「会议内容」面板**还没进
   * DOM**（惰性渲染非激活面板）。而转写此时通常早就加载完了
   * （`contentSettled === true`），于是 hook 会判定「内容已定稿但容器不存在」
   * = **不可定位**，立刻弹出「原始记录暂不可定位」。
   *
   * 那是个假故障：不是记录找不到，只是面板还没挂载。它属于**调用方**才知道的
   * 第三种情况（hook 只知道「还在加载」与「根本没有转写」），所以修正放在这里。
   *
   * 实测（本 Step 浏览器 E2E）：`useEffect` 依赖 `activeTab` 也**不够** ——
   * AntD 的同步发生在更晚的一次 commit，effect 跑的时候 ref 仍是 null。可靠
   * 的信号是**节点挂载本身**，所以用 callback ref：无论面板在哪一次 commit
   * 落地，节点「出现」的那一刻都会回调，这才是「可以定位了」的真实时刻。
   */
  const pendingTraceRef = useRef<number[] | null>(null)
  /** 容器挂载状态。把 ref 的存在性提升为 state，effect 才有可依赖的触发点。 */
  const [transcriptNode, setTranscriptNode] = useState<HTMLDivElement | null>(null)

  const attachTranscript = useCallback((node: HTMLDivElement | null) => {
    transcriptRef.current = node
    setTranscriptNode(node)
  }, [])

  const handleTraceEvidence = useCallback(
    (seqs: number[]) => {
      // 已经在「会议内容」Tab 且容器已挂载：此刻容器可见，滚动计算有效，
      // 直接定位。（callback ref 只在挂载时触发，覆盖不到这种情况。）
      if (activeTab === 'transcript' && transcriptRef.current) {
        evidence.traceEvidence(seqs)
        return
      }
      pendingTraceRef.current = seqs
      setActiveTab('transcript')
    },
    [activeTab, evidence.traceEvidence],
  )

  useEffect(() => {
    // 两个条件缺一不可：目标 Tab 已激活（文本已可见）＋容器已挂载（有 DOM 可查）。
    // 容器挂载是个 state，所以面板晚到的那一次 commit 也会重新触发本 effect。
    if (activeTab !== 'transcript' || !transcriptNode) return
    const pending = pendingTraceRef.current
    if (!pending) return
    pendingTraceRef.current = null
    evidence.traceEvidence(pending)
    // 依赖 `traceEvidence` 本身（hook 内是稳定 useCallback），而不是整个
    // `evidence` 对象 —— 后者每次 render 都是新引用，会让本 effect 空转。
  }, [activeTab, transcriptNode, evidence.traceEvidence])

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
          <SummarySections summary={display} onTraceEvidence={handleTraceEvidence} />
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
      <div className="transcript-surface transcript-surface--top" ref={attachTranscript}>
        <ol className="transcript-segments">
          {segments.map((seg) => (
            <li
              key={seg.segment_seq}
              className="transcript-segment"
              /* 与 MeetingRoom 用同一个稳定标识：UI-INTEL-2A 建立的定位依据。
                 禁止用数组 index / 文本 / utterance_id —— 那些都会漂移。 */
              data-segment-seq={seg.segment_seq}
            >
              <span className="transcript-segment__speaker">{seg.speaker_id}</span>
              <span className="transcript-segment__text">{seg.text}</span>
            </li>
          ))}
        </ol>
        {/* 不可定位轻提示（§8），与 MeetingRoom 同一套文案 / 同一个 class */}
        {evidence.outcome === 'not_found' ? (
          <div className="transcript-note" role="status">原始记录暂不可定位</div>
        ) : null}
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
          activeKey={activeTab}
          onChange={setActiveTab}
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
