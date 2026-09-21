import { Button, Skeleton, Tag } from 'antd'
import {
  AudioOutlined,
  BulbOutlined,
  CheckCircleOutlined,
  ReloadOutlined,
  SyncOutlined,
} from '@ant-design/icons'
import { EmptyPlaceholder } from '@/components/EmptyPlaceholder'
import { EvidenceTraceButton } from '@/components/EvidenceTraceButton'
import type { MeetingState, MeetingStateStatus } from '@/types/meetingState'

/**
 * Meeting Intelligence Panel（Step UI-INTEL-1）。
 *
 * 展示 AI 在会议进行中**理解出的内容**：顶部是面向参会人的实时会议摘要，
 * 下方仍保留当前议题 / 已确认决策 / 待办 / 未决问题 / 风险等结构化工作记忆。
 * 数据全部来自 `GET /api/meetings/:id/state` —— 服务端
 * 持久化的 Meeting State。
 *
 * 三条展示层铁律：
 *  1. **绝不前端推导**：不用关键词规则从 Transcript 猜 decision / action item。
 *     没有就是没有 —— 展示「暂无」，而不是猜一个。这里只做渲染。
 *  2. **「待确认」只属于 UI fallback**：`owner` / `deadline` 契约上可为 null
 *     （转写里确实没说），UI 显示「待确认」纯为可读性，**绝不写回后端**。
 *  3. **失败不清空**：有 last-good 内容时继续展示 + 轻量不可用提示，不闪烁成空白。
 *
 * 本组件不请求数据、不管 SSE、不碰 store —— 全部由调用方通过 props 传入。
 *
 * UI-INTEL-2A：每条 decision / action item / risk / open question 带「查看原文」
 * 入口，点击只把 `source_segment_seqs` 交给 `onTraceEvidence`。**没有 evidence
 * 的条目根本不渲染入口**（不是 disabled 假按钮）；定位逻辑不在这里，
 * 收在 `useTranscriptEvidenceNavigation`。会中证据链依赖的
 * `source_segment_seqs` 来自服务端 Meeting State，前端绝不自行推导。
 */

const STATUS_LABEL: Record<MeetingStateStatus, string> = {
  EMPTY: '等待内容',
  ACTIVE: '实时更新中',
  STALE: '更新稍有延迟',
  FAILED: '暂时不可用',
  FROZEN: '已冻结',
}

void STATUS_LABEL // 保留完整状态文案表（供后续复用）；当前 UI 走 statusTag

function statusTag(status: MeetingStateStatus) {
  switch (status) {
    case 'ACTIVE':
      return <Tag color="success">实时更新中</Tag>
    case 'STALE':
      return <Tag color="warning">更新稍有延迟</Tag>
    case 'FAILED':
      return <Tag color="warning">暂时不可用</Tag>
    case 'FROZEN':
      return <Tag color="blue">已冻结</Tag>
    default:
      return <Tag>等待内容</Tag>
  }
}

/** 一个可复用的条目块（标题 + 条数），空则不渲染 —— 不给空列表制造噪音。 */
function ListBlock({
  title,
  count,
  children,
}: {
  title: string
  count: number
  children: React.ReactNode
}) {
  if (count === 0) return null
  return (
    <div className="intel-block">
      <div className="intel-block__head">
        <span className="intel-block__title">{title}</span>
        <span className="intel-block__count">{count}</span>
      </div>
      <ul className="intel-list">{children}</ul>
    </div>
  )
}

/** 纯文本条目（decision / open question / risk 共用） */
function TextItem({
  text,
  sourceSegmentSeqs,
  onTrace,
}: {
  text: string
  sourceSegmentSeqs: readonly number[] | null | undefined
  onTrace: (seqs: number[]) => void
}) {
  return (
    <li className="intel-item">
      <span className="intel-item__text">{text}</span>
      <EvidenceTraceButton sourceSegmentSeqs={sourceSegmentSeqs} onTrace={onTrace} />
    </li>
  )
}

/** 待办条目：owner / deadline 为 null 时显示「待确认」（仅展示层 fallback） */
function ActionItemRow({
  task,
  owner,
  deadline,
  sourceSegmentSeqs,
  onTrace,
}: {
  task: string
  owner: string | null
  deadline: string | null
  sourceSegmentSeqs: readonly number[] | null | undefined
  onTrace: (seqs: number[]) => void
}) {
  return (
    <li className="intel-item">
      <span className="intel-item__text">{task}</span>
      <span className="intel-item__meta">
        <span className="intel-meta">负责人：{owner ?? '待确认'}</span>
        <span className="intel-meta">截止时间：{deadline ?? '待确认'}</span>
      </span>
      {/* 一条待办常由多句转写支撑（如 [4,5]）——按钮文案用「查看依据」更准确 */}
      <EvidenceTraceButton
        sourceSegmentSeqs={sourceSegmentSeqs}
        onTrace={onTrace}
        label="查看依据"
      />
    </li>
  )
}

/** 当前议题：title + summary（后端 current_topic 只给标题，此处用 topics 里的匹配项补全） */
function TopicItem({
  title,
  summary,
  sourceSegmentSeqs,
  onTrace,
}: {
  title: string
  summary: string
  sourceSegmentSeqs: readonly number[] | null | undefined
  onTrace: (seqs: number[]) => void
}) {
  return (
    <li className="intel-item">
      <span className="intel-item__text">{title}</span>
      {summary ? <span className="intel-item__sub">{summary}</span> : null}
      <EvidenceTraceButton sourceSegmentSeqs={sourceSegmentSeqs} onTrace={onTrace} />
    </li>
  )
}

interface MeetingIntelligencePanelProps {
  state: MeetingState | null
  loading: boolean
  error: string
  isMeetingLive: boolean
  pendingSegmentCount: number
  onRefresh: () => void
  /**
   * 「查看原文」回调（Step UI-INTEL-2A）。只把 `source_segment_seqs` 交给调用方，
   * 组件本身不做定位、不碰 DOM —— 定位能力收在
   * `useTranscriptEvidenceNavigation`（§8：不在每个 item 里复制 querySelector）。
   */
  onTraceEvidence: (sourceSegmentSeqs: number[]) => void
}

function IntelligenceHeader({ status }: { status?: MeetingStateStatus }) {
  return (
    <div className="intel-head">
      <div>
        <div className="intel-eyebrow">MEETING INTELLIGENCE</div>
        <div className="intel-title">
          <BulbOutlined aria-hidden />
          AI 会议理解
        </div>
      </div>
      {status ? statusTag(status) : null}
    </div>
  )
}

function IntelligenceActivity({
  loading,
  isMeetingLive,
  pendingSegmentCount,
  status,
}: {
  loading: boolean
  isMeetingLive: boolean
  pendingSegmentCount: number
  status?: MeetingStateStatus
}) {
  const unavailable = status === 'FAILED' || status === 'STALE'
  const working =
    isMeetingLive && !unavailable && (loading || pendingSegmentCount > 0 || status === 'EMPTY')
  let icon = <CheckCircleOutlined aria-hidden />
  let title = isMeetingLive ? '已跟进到最新内容' : '本次理解已归档'
  let detail = isMeetingLive ? 'AI 将继续监听后续讨论' : '可通过下方条目查看对应原文'

  if (loading) {
    icon = <SyncOutlined spin aria-hidden />
    title = '正在同步最新理解'
    detail = '已有内容保持可见，完成后会自动更新'
  } else if (status === 'FAILED') {
    icon = <ReloadOutlined aria-hidden />
    title = '理解更新暂时中断'
    detail = '当前继续展示最近一次成功整理的内容'
  } else if (status === 'STALE') {
    icon = <SyncOutlined aria-hidden />
    title = '理解更新稍有延迟'
    detail = '系统仍在保留并展示最近一次可靠结果'
  } else if (isMeetingLive && pendingSegmentCount > 0) {
    icon = <AudioOutlined aria-hidden />
    title = `已收到 ${pendingSegmentCount} 条新内容`
    detail = '正在倾听并等待合适的整理时机'
  } else if (isMeetingLive && status === 'EMPTY') {
    icon = <AudioOutlined aria-hidden />
    title = '正在倾听会议'
    detail = '出现有效讨论后会自动形成摘要与要点'
  }

  return (
    <div className={`intel-activity${working ? ' intel-activity--working' : ''}`} role="status">
      <span className="intel-activity__icon">{icon}</span>
      <span className="intel-activity__copy">
        <strong>{title}</strong>
        <span>{detail}</span>
      </span>
      {working ? (
        <span className="intel-activity__signal" aria-hidden>
          <i />
          <i />
          <i />
        </span>
      ) : null}
    </div>
  )
}

export function MeetingIntelligencePanel({
  state,
  loading,
  error,
  isMeetingLive,
  pendingSegmentCount,
  onRefresh,
  onTraceEvidence,
}: MeetingIntelligencePanelProps) {
  // ---- 首次加载（还没有任何可用内容）----
  if (!state && loading) {
    return (
      <section className="intelligence-panel">
        <IntelligenceHeader />
        <IntelligenceActivity
          loading
          isMeetingLive={isMeetingLive}
          pendingSegmentCount={pendingSegmentCount}
        />
        <div className="intel-scroll">
          <Skeleton active paragraph={{ rows: 5 }} title={false} />
        </div>
      </section>
    )
  }

  // ---- 首次加载失败（无 last-good 可展示）----
  if (!state && error) {
    return (
      <section className="intelligence-panel">
        <IntelligenceHeader />
        <div className="intel-scroll">
          <EmptyPlaceholder
            icon={<ReloadOutlined />}
            title={error === 'not_found' ? '会议不存在' : '暂时无法加载'}
            description={
              error === 'not_found'
                ? '请确认会议链接是否正确。'
                : 'AI 会议理解暂时不可用，不影响实时转写。'
            }
            action={
              error === 'not_found' ? undefined : (
                <Button size="small" icon={<ReloadOutlined />} onClick={onRefresh}>
                  重试
                </Button>
              )
            }
            compact
          />
        </div>
      </section>
    )
  }

  if (!state) {
    return (
      <section className="intelligence-panel">
        <IntelligenceHeader />
        <IntelligenceActivity
          loading={false}
          isMeetingLive={isMeetingLive}
          pendingSegmentCount={pendingSegmentCount}
        />
        <div className="intel-scroll">
          <EmptyPlaceholder
            icon={<BulbOutlined />}
            title="等待会议内容"
            description="开始会议后，AI 会持续概括讨论内容，并整理议题、决策与待办。"
            compact
          />
        </div>
      </section>
    )
  }

  const {
    version,
    status,
    live_summary,
    current_topic,
    topics,
    decisions,
    action_items,
    open_questions,
    risks,
  } = state

  // EMPTY：会议已开始但尚未处理任何 FINAL —— 不是错误，是正常的早期状态。
  const isEmpty = status === 'EMPTY'
  const hasLiveSummary = Boolean(live_summary?.trim())
  const hasStructuredContent =
    Boolean(current_topic) ||
    topics.length > 0 ||
    decisions.length > 0 ||
    action_items.length > 0 ||
    open_questions.length > 0 ||
    risks.length > 0
  const hasAnyContent = hasLiveSummary || hasStructuredContent

  // 冲突的「当前议题」：后端给的是标题，这里只从 topics 里取同名项的 summary。
  // current_topic 命中 topics 时用 TopicItem（带 summary），否则退化为纯文本条目。
  const currentTopicMatch = current_topic
    ? topics.find((t) => t.title === current_topic)
    : undefined
  const discussedTopics = currentTopicMatch
    ? topics.filter((t) => t !== currentTopicMatch)
    : topics

  return (
    <section className="intelligence-panel">
      <IntelligenceHeader status={status} />
      <IntelligenceActivity
        loading={loading}
        isMeetingLive={isMeetingLive}
        pendingSegmentCount={pendingSegmentCount}
        status={status}
      />

      <div className="intel-scroll">

      {/* FAILED：保留已展示内容 + 轻量不可用提示（Meeting 仍可能是 COMPLETED，
          两者是独立维度）。绝不因为 FAILED 就清空用户已经看到的内容。 */}
      {status === 'FAILED' ? (
        <div className="intel-notice">
          状态更新暂时中断，以下为最近一次成功理解的内容。
        </div>
      ) : null}

      {status === 'STALE' ? (
        <div className="intel-notice intel-notice--soft">
          AI 摘要更新稍有延迟，以下内容仍为最近一次成功理解的结果。
        </div>
      ) : null}

      {/* 请求失败但有 last-good：同样保留内容，仅提示 */}
      {error && status !== 'FAILED' ? (
        <div className="intel-notice intel-notice--soft">
          最近一次刷新失败，正在展示上次内容。
          <Button type="link" size="small" onClick={onRefresh}>
            重试
          </Button>
        </div>
      ) : null}

      {isEmpty || !hasAnyContent ? (
        <EmptyPlaceholder
          icon={<BulbOutlined />}
          title={isEmpty ? '等待会议内容' : '暂时没有明确的结论'}
          description={
            isEmpty
              ? 'AI 会在会议进行中实时概括讨论内容，并逐步整理议题、决策与待办。'
              : 'AI 已收到内容，正在形成可读的会议摘要。'
          }
          compact
        />
      ) : (
        <>
          {hasLiveSummary ? (
            <div className="intel-summary" aria-live="polite">
              <div className="intel-summary__label">实时摘要</div>
              <div className="intel-summary__text">{live_summary}</div>
            </div>
          ) : null}

          {current_topic ? (
            <div className="intel-block">
              <div className="intel-block__head">
                <span className="intel-block__title">当前议题</span>
              </div>
              <ul className="intel-list">
                {currentTopicMatch ? (
                  <TopicItem
                    title={currentTopicMatch.title}
                    summary={currentTopicMatch.summary}
                    sourceSegmentSeqs={currentTopicMatch.source_segment_seqs}
                    onTrace={onTraceEvidence}
                  />
                ) : (
                  /* current_topic 是标量、结构性没有 evidence（§8：不为它单独做）。
                     命中 topics 时走上面的 TopicItem 才有证据入口。 */
                  <li className="intel-item">
                    <span className="intel-item__text">{current_topic}</span>
                  </li>
                )}
              </ul>
            </div>
          ) : null}

          <ListBlock title="已讨论议题" count={discussedTopics.length}>
            {discussedTopics.map((topic, i) => (
              <TopicItem
                key={`t-${i}`}
                title={topic.title}
                summary={topic.summary}
                sourceSegmentSeqs={topic.source_segment_seqs}
                onTrace={onTraceEvidence}
              />
            ))}
          </ListBlock>

          <ListBlock title="已确认决策" count={decisions.length}>
            {decisions.map((d, i) => (
              <TextItem
                key={`d-${i}`}
                text={d.text}
                sourceSegmentSeqs={d.source_segment_seqs}
                onTrace={onTraceEvidence}
              />
            ))}
          </ListBlock>

          <ListBlock title="待办事项" count={action_items.length}>
            {action_items.map((a, i) => (
              <ActionItemRow
                key={`a-${i}`}
                task={a.task}
                owner={a.owner}
                deadline={a.deadline}
                sourceSegmentSeqs={a.source_segment_seqs}
                onTrace={onTraceEvidence}
              />
            ))}
          </ListBlock>

          <ListBlock title="未决问题" count={open_questions.length}>
            {open_questions.map((q, i) => (
              <TextItem
                key={`q-${i}`}
                text={q.text}
                sourceSegmentSeqs={q.source_segment_seqs}
                onTrace={onTraceEvidence}
              />
            ))}
          </ListBlock>

          <ListBlock title="风险 / 顾虑" count={risks.length}>
            {risks.map((r, i) => (
              <TextItem
                key={`r-${i}`}
                text={r.text}
                sourceSegmentSeqs={r.source_segment_seqs}
                onTrace={onTraceEvidence}
              />
            ))}
          </ListBlock>
        </>
      )}

      {/* version 仍用于请求乱序守卫，但产品层不暴露工程版本号。 */}
        {version > 0 ? (
          <div className="intel-foot">
            已理解至第 {state.last_processed_segment_seq} 条确认内容
          </div>
        ) : null}
      </div>
    </section>
  )
}
