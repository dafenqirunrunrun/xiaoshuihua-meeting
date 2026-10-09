import { Button, Skeleton, Tag } from 'antd'
import {
  BulbOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  ReloadOutlined,
  SyncOutlined,
  ThunderboltOutlined,
  WarningOutlined,
} from '@ant-design/icons'
import { EmptyPlaceholder } from '@/components/EmptyPlaceholder'
import { EvidenceTraceButton } from '@/components/EvidenceTraceButton'
import { CAPTAIN_LABEL } from '@/constants/captain'
import type { CaptainQuickEventPayload } from '@/hooks/useMeetingStream'
import type {
  CaptainDeepAssessment,
  CaptainEvidenceItem,
  CaptainExternalEvidence,
  CaptainPointItem,
  CaptainQuickAssessment,
  CaptainReportPublic,
  CaptainReportStatus,
} from '@/types/captain'

/**
 * 水华队长裁判面板（ADR-002 前端接入）。
 *
 * 数据全部来自 `GET /api/meetings/:id/captain/latest`（或按 report_id）—— 服务端
 * 持久化的报告是**唯一事实源**；`captain.completed` SSE 只是快路径，断线 / 刷新
 * 一律回到 GET 恢复。
 *
 * 四条展示层铁律（与 MeetingIntelligencePanel 同一纪律，且受 ADR-002 明确约束）：
 *  1. **绝不前端推导**：不在前端从转写里"提炼"观点或分歧。没有就是没有。
 *  2. **禁止两方叙事**：`positions` 是模型自主识别的 1..N 个观点簇，**不是**
 *     正方/反方。UI 不得给某一条打上"A 方"「支持」之类的立场标签 ——
 *     本系统的话者身份不可靠，ADR-002 禁止裁判输出承载人物归因。
 *  3. **空分歧是正常结果**：「不强行制造冲突」是结构性保证。`dispute_points`
 *     为空时如实显示「未发现分歧」，绝不写成「暂无数据」暗示缺内容。
 *  4. **失败不清空**：有 last-good 报告时继续展示，只加一条轻量提示。
 *
 * 本组件不请求数据、不管 SSE、不碰 store —— 全部由调用方通过 props 传入。
 */

const STATUS_TAG: Record<CaptainReportStatus, { color: string; label: string }> = {
  PENDING: { color: 'default', label: '排队中' },
  ANALYZING: { color: 'processing', label: '分析中' },
  COMPLETED: { color: 'success', label: '已完成' },
  FAILED: { color: 'warning', label: '未能完成' },
}

function statusTag(status: CaptainReportStatus) {
  const t = STATUS_TAG[status] ?? STATUS_TAG.PENDING
  return <Tag color={t.color}>{t.label}</Tag>
}

/** 一个可复用的条目块：空则不渲染 —— 不给空列表制造噪音。 */
function Block({
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

interface CaptainPanelProps {
  report: CaptainReportPublic | null
  /**
   * 两阶段裁判第一段（Quick）的临时结果，来自 `captain.quick_assessment` SSE。
   *
   * **不是事实源**：报告一旦 COMPLETED / FAILED，它就会在 `useCaptainReferee`
   * 里被清空，届时应改读 `report.report.quick` / `.deep`。两者永不同时有效 ——
   * 这正是「一次裁判只有一种说法」的保证。
   */
  quick: CaptainQuickEventPayload | null
  loading: boolean
  triggering: boolean
  error: string
  /** 会议是否处于可裁判状态（IN_PROGRESS / FINALIZING） */
  canTrigger: boolean
  onTrigger: () => void
  onRefresh: () => void
  onTraceEvidence: (sourceSegmentSeqs: number[]) => void
}

function CaptainActivity({
  report,
  loading,
  triggering,
}: {
  report: CaptainReportPublic | null
  loading: boolean
  triggering: boolean
}) {
  const status = report?.status
  const busy = triggering || status === 'PENDING' || status === 'ANALYZING'

  let icon = <CheckCircleOutlined aria-hidden />
  let title = '裁判已完成'
  let detail = '以下是本次讨论的观点与分歧梳理'

  if (triggering) {
    icon = <SyncOutlined spin aria-hidden />
    title = '正在请求水华队长'
    detail = '请求已受理，正在准备分析最近的讨论'
  } else if (status === 'PENDING') {
    icon = <ClockCircleOutlined aria-hidden />
    title = '排队等待分析'
    detail = '水华队长正在处理另一项分析，稍后轮到本次'
  } else if (status === 'ANALYZING') {
    icon = <SyncOutlined spin aria-hidden />
    title = '正在梳理讨论'
    detail = '正在阅读最近的发言并整理观点与分歧'
  } else if (status === 'FAILED') {
    icon = <WarningOutlined aria-hidden />
    title = '本次未能给出报告'
    detail = '材料可能不足，或分析过程出现问题。可稍后重新请求'
  } else if (loading && !report) {
    icon = <SyncOutlined spin aria-hidden />
    title = '正在读取裁判记录'
    detail = '已有内容保持可见'
  }

  return (
    <div className={`intel-activity${busy ? ' intel-activity--working' : ''}`} role="status">
      <span className="intel-activity__icon">{icon}</span>
      <span className="intel-activity__copy">
        <strong>{title}</strong>
        <span>{detail}</span>
      </span>
      {busy ? (
        <span className="intel-activity__signal" aria-hidden>
          <i />
          <i />
          <i />
        </span>
      ) : null}
    </div>
  )
}

/**
 * 两阶段裁判第一段（Quick）的**临时**卡片。
 *
 * 三条刻意为之：
 *  1. 明确标注「临时 / 正在深入核对」—— 它**不是**最终结论，UI 不得让读者
 *     以为裁判已经说完了。视觉上以 soft notice 呈现，与 durable 报告区分。
 *  2. `question_to_resolve` 单独突出：这是 Quick 阶段最有用的一行 —— 它告诉
 *     会议室「去确认什么就能推动讨论」，比临时判断更接近行动。
 *  3. **不给它的话加任何「结论」措辞**，只呈现模型原文；临时判断是模型的
 *     原话，前端不加工、不总结（铁律 1）。
 */
function QuickProvisionalCard({
  quick,
  onTraceEvidence,
}: {
  quick: CaptainQuickEventPayload
  onTraceEvidence: (sourceSegmentSeqs: number[]) => void
}) {
  return (
    <div className="captain-quick" role="status" aria-live="polite">
      <div className="captain-quick__head">
        <span className="captain-quick__badge">
          <SyncOutlined spin aria-hidden />
          临时判断
        </span>
        <span className="captain-quick__hint">正在深入核对，稍后会有完整报告</span>
      </div>

      <div className="captain-quick__issue">{quick.issue}</div>

      <div className="captain-quick__label">当前看法</div>
      <div className="captain-quick__judgement">{quick.provisional_judgement}</div>

      {quick.key_evidence.length > 0 ? (
        <ul className="captain-quick__evidence">
          {quick.key_evidence.map((e, i) => (
            <li key={`qe-${i}`} className="intel-item">
              <span className="captain-quote">「{e.quote}」</span>
              <span className="intel-item__sub">→ {e.supports}</span>
              <EvidenceTraceButton
                sourceSegmentSeqs={[e.segment_seq]}
                onTrace={onTraceEvidence}
              />
            </li>
          ))}
        </ul>
      ) : null}

      <div className="captain-quick__resolve">
        <span className="captain-quick__resolve-label">最值得先确认</span>
        <span className="captain-quick__resolve-text">{quick.question_to_resolve}</span>
      </div>
    </div>
  )
}

/**
 * Deep 阶段的推理正文。
 *
 * 排序即论点：issue → 主张与各自证据 → 反证 → 已确认事实 / 假设 / 缺失材料
 * → 证据强度 → 判断与建议。这个顺序让读者**先看材料再看结论**，
 * 与「禁止只喂摘要」是同一条纪律在展示层的延伸。
 *
 * 两个必须存在的诚实装置：
 *  1. `supersedes` 非空时单独渲染「已修正」提示。Deep 允许推翻 Quick，
 *     但契约要求非静默 —— UI 若把它埋在正文里，就等于让读者自己发现矛盾，
 *     等于静默反转。
 *  2. `assumptions` / `missing_evidence` 即便为空也**不隐藏**整块标题的语义
 *     —— 但这里沿用 Block（空则不渲染）以免制造噪音：一块空的「假设」
 *     会让人以为「没有假设」，而事实可能是模型没填。所以这一块只在非空时
 *     出现，这是与 `dispute_points` 不同的处理（后者空 = 无分歧本身是结论）。
 */
function DeepAssessmentBlock({
  deep,
  onTraceEvidence,
}: {
  deep: CaptainDeepAssessment
  onTraceEvidence: (sourceSegmentSeqs: number[]) => void
}) {
  const quotes = (items: CaptainEvidenceItem[], title: string) => (
    <Block title={title} count={items.length}>
      {items.map((e, i) => (
        <li key={`${title}-${i}`} className="intel-item">
          <span className="captain-quote">「{e.quote}」</span>
          <span className="intel-item__sub">→ {e.supports}</span>
          <EvidenceTraceButton
            sourceSegmentSeqs={[e.segment_seq]}
            onTrace={onTraceEvidence}
          />
        </li>
      ))}
    </Block>
  )

  const points = (items: CaptainPointItem[], title: string) => (
    <Block title={title} count={items.length}>
      {items.map((p, i) => (
        <li key={`${title}-${i}`} className="intel-item">
          <span className="intel-item__text">{p.text}</span>
          <EvidenceTraceButton
            sourceSegmentSeqs={p.source_segment_seqs}
            onTrace={onTraceEvidence}
          />
        </li>
      ))}
    </Block>
  )

  return (
    <div className="captain-deep">
      {deep.supersedes ? (
        <div className="captain-deep__supersede">
          <div className="captain-deep__supersede-label">已修正先前的临时判断</div>
          <div className="captain-deep__supersede-text">{deep.supersedes}</div>
        </div>
      ) : null}

      <div className="captain-deep__issue">
        <span className="intel-summary__label">核心争议</span>
        <span className="captain-deep__issue-text">{deep.issue}</span>
      </div>

      {deep.claims.length > 0 ? (
        <Block title="各方主张" count={deep.claims.length}>
          {deep.claims.map((c, i) => (
            <li key={`c-${i}`} className="intel-item">
              <span className="captain-position__label">{c.label}</span>
              <span className="intel-item__text">{c.statement}</span>
              <EvidenceTraceButton
                sourceSegmentSeqs={c.source_segment_seqs}
                onTrace={onTraceEvidence}
              />
            </li>
          ))}
        </Block>
      ) : null}

      {quotes(deep.evidence, '支持证据')}
      {quotes(deep.counter_evidence, '反证')}
      {points(deep.confirmed_facts, '已确认事实')}

      {/* 证据强度：等级 + 依据，**无百分比**。 */}
      {deep.evidence_strength.length > 0 ? (
        <Block title="证据强度" count={deep.evidence_strength.length}>
          {deep.evidence_strength.map((s, i) => (
            <li key={`s-${i}`} className="intel-item">
              <span className="captain-strength">
                <span className="captain-strength__claim">{s.claim}</span>
                <span className="captain-strength__level">{s.strength}</span>
              </span>
              <span className="intel-item__sub">{s.basis}</span>
              <EvidenceTraceButton
                sourceSegmentSeqs={s.source_segment_seqs}
                onTrace={onTraceEvidence}
              />
            </li>
          ))}
        </Block>
      ) : null}

      {points(deep.missing_evidence, '缺失材料')}

      {deep.assumptions.length > 0 ? (
        <Block title="待验证的假设" count={deep.assumptions.length}>
          {deep.assumptions.map((a, i) => (
            <li key={`as-${i}`} className="intel-item">
              <span className="intel-item__text">{a.text}</span>
              <EvidenceTraceButton
                sourceSegmentSeqs={a.source_segment_seqs}
                onTrace={onTraceEvidence}
              />
            </li>
          ))}
        </Block>
      ) : null}

      <div className="captain-deep__judgement">
        <span className="intel-summary__label">判断</span>
        <span className="captain-deep__judgement-text">{deep.judgement}</span>
      </div>

      <div className="captain-uncertainty captain-uncertainty--suggestion">
        <div className="captain-uncertainty__label">建议的下一步</div>
        <div className="captain-uncertainty__text">{deep.recommendation}</div>
      </div>

      {quotes(deep.citations, '引用')}
    </div>
  )
}

/**
 * 外部资料（Phase 3-2 / Phase C.5）。
 *
 * 四条展示纪律：
 *  1. **与转写证据视觉分区**：它没有 `segment_seq`，因此**没有**「查看原文」
 *     按钮 —— 一份网页不是会议内容，把它渲染得像转写片段就是在制造错误的
 *     权威。这里只给外链。
 *  2. `source_quality` 是**启发式**（按域名），UI 原样展示等级，不加工、
 *     不做「更可信」的排序措辞 —— 系统无法核实网页内容。
 *  3. `source_type` 是**另一个轴**（这是什么 vs 多可信），与 quality 并列展示，
 *     且都用原值：UI 不替读者把 `community` 解读成「不可信」。
 *  4. 优先显示 `evidence_summary`（流水线的检索结果级摘要）而非 `context`
 *     （provider 原始片段）——两者都是**检索结果级**，系统从未抓取网页正文。
 *     摘要缺失时回退到 context，绝不显示一个空条目。
 */
function ExternalEvidenceBlock({
  items,
}: {
  items: CaptainExternalEvidence[]
}) {
  if (items.length === 0) return null
  return (
    <Block title="外部资料" count={items.length}>
      {items.map((e) => (
        <li key={e.source_id} className="intel-item">
          <span className="captain-ext">
            <span className="captain-ext__id">[{e.source_id}]</span>
            <a
              className="captain-ext__title"
              href={e.url}
              target="_blank"
              rel="noreferrer noopener"
            >
              {e.title || e.domain || e.url}
            </a>
          </span>
          {e.evidence_summary || e.context ? (
            <span className="intel-item__sub">
              {e.evidence_summary || e.context}
            </span>
          ) : null}
          <span className="intel-item__sub">
            {e.domain}
            {e.source_quality ? ` · 来源层级：${e.source_quality}` : ''}
            {e.source_type ? ` · 类型：${e.source_type}` : ''}
            {e.published_at ? ` · 发布：${e.published_at}` : ''}
            {e.answers_question ? ` · 回答：${e.answers_question}` : ''}
          </span>
        </li>
      ))}
    </Block>
  )
}

/**
 * 落盘报告里的第一段判断（Quick）—— 权威版本，不是 SSE 快照。
 *
 * 与 `QuickProvisionalCard` 的区别有两点，都是刻意的：
 *  1. 不再有「正在深入核对」的转圈徽章 —— 报告已终态，那样写是谎话。
 *  2. 若 Deep 修正了它（`supersededBy` 非空），此处显式标注**已被修正**并
 *     把修正说明放在同一条目内。这样读者在同一视野里同时看到「当时的初步
 *     判断」与「后来改成了什么」，而不用在两块之间自己连线。
 */
function DurableQuickBlock({
  quick,
  supersededBy,
  onTraceEvidence,
}: {
  quick: CaptainQuickAssessment
  supersededBy: string
  onTraceEvidence: (sourceSegmentSeqs: number[]) => void
}) {
  return (
    <div className="captain-quick captain-quick--durable">
      <div className="captain-quick__head">
        <span className="captain-quick__badge">
          {supersededBy ? '初步判断（已被修正）' : '初步判断'}
        </span>
      </div>

      <div className="captain-quick__issue">{quick.issue}</div>

      <div className="captain-quick__label">当时的判断</div>
      <div className="captain-quick__judgement">{quick.provisional_judgement}</div>

      {quick.key_evidence.length > 0 ? (
        <ul className="captain-quick__evidence">
          {quick.key_evidence.map((e, i) => (
            <li key={`dq-${i}`} className="intel-item">
              <span className="captain-quote">「{e.quote}」</span>
              <span className="intel-item__sub">→ {e.supports}</span>
              <EvidenceTraceButton
                sourceSegmentSeqs={[e.segment_seq]}
                onTrace={onTraceEvidence}
              />
            </li>
          ))}
        </ul>
      ) : null}

      {supersededBy ? (
        <div className="captain-quick__resolve">
          <span className="captain-quick__resolve-label">后续修正为</span>
          <span className="captain-quick__resolve-text">{supersededBy}</span>
        </div>
      ) : (
        <div className="captain-quick__resolve">
          <span className="captain-quick__resolve-label">最值得先确认</span>
          <span className="captain-quick__resolve-text">
            {quick.question_to_resolve}
          </span>
        </div>
      )}
    </div>
  )
}

export function CaptainPanel({
  report,
  quick,
  loading,
  triggering,
  error,
  canTrigger,
  onTrigger,
  onRefresh,
  onTraceEvidence,
}: CaptainPanelProps) {
  const content = report?.report ?? null

  const header = (
    <div className="intel-head">
      <div>
        <div className="intel-eyebrow">WATER FLOWER CAPTAIN</div>
        <div className="intel-title">
          <ThunderboltOutlined aria-hidden />
          {CAPTAIN_LABEL}
        </div>
      </div>
      {report ? statusTag(report.status) : null}
    </div>
  )

  /** 请求按钮：所有分支共用，状态文案随报告状态变化。 */
  const triggerButton = (
    <Button
      type="primary"
      size="small"
      className="captain-panel__trigger"
      icon={<ThunderboltOutlined />}
      loading={triggering}
      disabled={!canTrigger}
      onClick={onTrigger}
    >
      {report ? '再分析一次' : '请求分析'}
    </Button>
  )

  // ---- 首次加载（还没有任何报告）----
  if (!report && loading) {
    return (
      <section className="intelligence-panel captain-panel">
        {header}
        <CaptainActivity report={null} loading triggering={false} />
        <div className="intel-scroll">
          <Skeleton active paragraph={{ rows: 5 }} title={false} />
        </div>
      </section>
    )
  }

  // ---- 从未被裁判过（后端 404，正常状态）----
  if (!report && (error === 'no_report' || error === '')) {
    return (
      <section className="intelligence-panel captain-panel">
        {header}
        <CaptainActivity report={null} loading={false} triggering={triggering} />
        <div className="intel-scroll">
          <EmptyPlaceholder
            icon={<ThunderboltOutlined />}
            title="还没有裁判记录"
            description={
              canTrigger
                ? `讨论出现分歧或需要梳理时，说一声「${CAPTAIN_LABEL}」或点下方按钮，让裁判分析最近的讨论。`
                : '会议结束后不再接受新的裁判请求。'
            }
            action={canTrigger ? triggerButton : undefined}
            compact
          />
        </div>
      </section>
    )
  }

  // ---- 读取失败（无 last-good）----
  if (!report && error) {
    const message =
      error === 'throttled'
        ? '刚刚已经分析过一次，请稍候再试。'
        : error === 'not_found'
          ? '请确认会议链接是否正确。'
          : error === 'busy'
            ? '水华队长正忙，请稍后重试。'
            : '水华队长暂时不可用，不影响实时转写。'
    return (
      <section className="intelligence-panel captain-panel">
        {header}
        <div className="intel-scroll">
          <EmptyPlaceholder
            icon={<ReloadOutlined />}
            title="暂时无法请求裁判"
            description={message}
            action={
              <Button size="small" icon={<ReloadOutlined />} onClick={onRefresh}>
                重试
              </Button>
            }
            compact
          />
        </div>
      </section>
    )
  }

  if (!report) return null

  // ---- 有报告（可能还在跑，或已 FAILED）----
  const failed = report.status === 'FAILED'
  const running = report.status === 'PENDING' || report.status === 'ANALYZING'

  return (
    <section className="intelligence-panel captain-panel">
      {header}
      <CaptainActivity report={report} loading={loading} triggering={triggering} />

      <div className="intel-scroll">
        {/* 有 last-good 内容时刷新失败：保留内容，仅提示（铁律 4） */}
        {error && error !== 'no_report' ? (
          <div className="intel-notice intel-notice--soft">
            最近一次刷新失败，正在展示上次的裁判结果。
            <Button type="link" size="small" onClick={onRefresh}>
              重试
            </Button>
          </div>
        ) : null}

        {failed ? (
          <div className="intel-notice">
            本次分析未能生成报告（材料不足或分析中断）。失败不会留下半份报告 ——
            重新请求即可再试。
          </div>
        ) : null}

        {running ? (
          <div className="intel-notice intel-notice--soft">
            {quick
              ? '第一段判断已给出（见下），完整报告仍在生成。'
              : '分析完成后，报告会出现在这里，无需刷新页面。'}
          </div>
        ) : null}

        {/* Task 6：两阶段裁判的第一段先到 —— 报告仍未落盘时才展示，
            且**不**替换任何 durable 内容（此时 content 必为 null）。
            `quick` 与 `content` 在 hook 层被保证互斥。 */}
        {quick && !content ? (
          <QuickProvisionalCard quick={quick} onTraceEvidence={onTraceEvidence} />
        ) : null}

        {content ? (
          <>
            {/* 讨论概要 */}
            <div className="captain-summary">
              <div className="intel-summary__label">讨论概要</div>
              <div className="intel-summary__text">{content.discussion_summary}</div>
            </div>

            {/* 两阶段裁判的 Deep 结果（Task 3-5）。
                刻意**先于**下面的扁平字段渲染：当 deep 存在时，扁平字段是从
                deep 派生出来的（后端 `_assemble_staged_content`），两者说的是
                同一件事。先给读者推理过程（claims / evidence / 证据强度），
                再给结论；反过来读会先看到结论，再看不出结论是怎么来的。
                单通道路径（deep 为 null）下本节整块跳过，扁平字段原样渲染。 */}
            {content.deep ? (
              <DeepAssessmentBlock
                deep={content.deep}
                onTraceEvidence={onTraceEvidence}
              />
            ) : null}

            {/* 落盘报告里的第一段判断（Task 6）。
                与上面的 SSE 临时卡片不同：这里是**权威版本**，报告已终态。
                Deep 修正了它时（supersedes 非空）必须一并展示 —— 否则读者
                只看到修正后的结论，看不到被修正的是什么，等于把一次显式
                修正又变回静默修正。 */}
            {content.quick ? (
              <DurableQuickBlock
                quick={content.quick}
                supersededBy={content.deep?.supersedes ?? ''}
                onTraceEvidence={onTraceEvidence}
              />
            ) : null}

            {/* 观点：1..N 个，非「两方」。label 是中性短标签（如「成本优先」），
                不是人名、也不标注立场。 */}
            <Block title="讨论中的观点" count={content.positions.length}>
              {content.positions.map((p, i) => (
                <li key={`p-${i}`} className="intel-item">
                  <span className="captain-position__label">{p.label}</span>
                  <span className="intel-item__text">{p.statement}</span>
                  <EvidenceTraceButton
                    sourceSegmentSeqs={p.source_segment_seqs}
                    onTrace={onTraceEvidence}
                  />
                </li>
              ))}
            </Block>

            {/* 共识 */}
            <Block title="共识" count={content.agreement_points.length}>
              {content.agreement_points.map((a, i) => (
                <li key={`a-${i}`} className="intel-item">
                  <span className="intel-item__text">{a.text}</span>
                  <EvidenceTraceButton
                    sourceSegmentSeqs={a.source_segment_seqs}
                    onTrace={onTraceEvidence}
                  />
                </li>
              ))}
            </Block>

            {/* 分歧：允许为空 —— 此时如实说明「未发现分歧」，
                而不是显示一个空块或「暂无数据」（铁律 3）。 */}
            {content.dispute_points.length > 0 ? (
              <Block title="分歧" count={content.dispute_points.length}>
                {content.dispute_points.map((d, i) => (
                  <li key={`d-${i}`} className="intel-item">
                    <span className="intel-item__text">{d.text}</span>
                    <EvidenceTraceButton
                      sourceSegmentSeqs={d.source_segment_seqs}
                      onTrace={onTraceEvidence}
                    />
                  </li>
                ))}
              </Block>
            ) : (
              <div className="intel-block">
                <div className="intel-block__head">
                  <span className="intel-block__title">分歧</span>
                </div>
                <div className="captain-quiet">本次讨论中未发现明确分歧。</div>
              </div>
            )}

            {/* 不确定性：必填且非空 —— 裁判必须说自己不知道什么。
                这里单独成一个视觉块，与「结论」区分开。 */}
            <div className="captain-uncertainty">
              <div className="captain-uncertainty__label">
                <BulbOutlined aria-hidden />
                不确定性
              </div>
              <div className="captain-uncertainty__text">{content.uncertainty}</div>
            </div>

            {/* 建议：只说「还缺什么信息 / 下一步确认什么」，不给结论性建议 */}
            <div className="captain-uncertainty captain-uncertainty--suggestion">
              <div className="captain-uncertainty__label">下一步</div>
              <div className="captain-uncertainty__text">{content.suggestion}</div>
            </div>

            {/* 引文：逐字引用，服务端已校验（伪造引文会被整条剔除） */}
            <Block title="原文依据" count={content.evidence.length}>
              {content.evidence.map((e, i) => (
                <li key={`e-${i}`} className="intel-item">
                  <span className="captain-quote">「{e.quote}」</span>
                  <span className="intel-item__sub">→ {e.supports}</span>
                  <EvidenceTraceButton
                    sourceSegmentSeqs={[e.segment_seq]}
                    onTrace={onTraceEvidence}
                  />
                </li>
              ))}
            </Block>

            {/* 外部资料：来自搜索流水线，与转写证据严格分区（无溯源按钮） */}
            <ExternalEvidenceBlock items={content.external_evidence} />
          </>
        ) : null}

        <div className="captain-panel__foot">
          {canTrigger ? triggerButton : null}
          {report.window_from_seq > 0 ? (
            <span className="intel-foot">
              {/* The window bounds are exact: the backend records what the model
                  actually read. Whether the LAST segment is inside depends on
                  the trigger — a wake phrase is excluded, a button-press
                  high-water mark is content and stays. The UI does not try to
                  re-derive that (it would be a second owner of the rule), it
                  just reports the bounds it was given. */}
              {report.window_from_seq === report.window_to_seq
                ? `分析范围：第 ${report.window_from_seq} 条`
                : `分析范围：第 ${report.window_from_seq}–${report.window_to_seq} 条`}
            </span>
          ) : null}
        </div>
      </div>
    </section>
  )
}
