import { useEffect, useRef, useState } from 'react'
import { PixelCaptain, type PixelCaptainPhase } from '@/components/PixelCaptain'
import { CAPTAIN_LABEL } from '@/constants/captain'
import type { CaptainReportPublic } from '@/types/captain'

/**
 * 水华队长像素角色（Phase 3-1）—— 会议页的**状态可视化**，不是数据源。
 *
 * 这个组件只做三件事：
 *  1. 把 `phase`（由 `usePixelCaptainState` 从真实 captain 字段派生）画成一个
 *     有辨识度的像素小人；
 *  2. 在 COMPLETED 时提供「展开报告 / 收起报告」这一个动作；
 *  3. 其余时间什么都不做 —— 它不请求数据、不管 SSE、不碰 store。
 *
 * 五条不允许破的线
 * ----------------
 *  1. **不摆假状态**：没有任何计时器。COMPLETED 只在服务端真的落库了报告时出现。
 *  2. **不暴露内部错误**：FAILED 的文案是固定的产品语言（「本次分析未完成」），
 *     绝不打印 provider / backend / 堆栈信息。`report` 之外的错误字符串只被
 *     `usePixelCaptainState` 读过一次（为了把 `no_report` 排除在失败之外），
 *     从不显示。
 *  3. **不遮挡内容**：角色与说明**全部在文档流内**（没有 absolute / fixed），
 *     所以它既不会盖住左边的转写，也不会盖住底部的演示输入条 —— 这一条是
 *     靠布局保证的，不是靠 z-index 调出来的。
 *  4. **不越权触发**：只在 `canTrigger` 时渲染「请求分析」按钮，与 CaptainPanel
 *     同一条件（会议结束后不再新建报告）。
 *  5. **不重复展示**：气泡只放一行说明和一个动作；完整报告仍然由 CaptainPanel
 *     渲染。角色负责「现在到哪一步了」，面板负责「报告说了什么」。
 */

interface PixelCaptainDockProps {
  phase: PixelCaptainPhase
  caption: string
  /** 会议是否处于可裁判状态（决定按钮是否可用） */
  canTrigger: boolean
  /** 是否有报告正文可展开 */
  hasReport: boolean
  expanded: boolean
  onToggle: () => void
  /** 请求一次裁判（走 POST /captain，与演示输入条的快捷发送是两条真实路径） */
  onTrigger?: () => void
  /** 正在触发（按钮 loading） */
  triggering: boolean
  /** 最近一份报告，仅用于显示判过的时刻；无报告时为 null */
  report: CaptainReportPublic | null
}

/** 只显示时分，避免在角色旁出现一长串 ISO 时间。 */
function timeText(iso: string | null): string {
  if (!iso) return ''
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) return ''
  return parsed.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

export function PixelCaptainDock({
  phase,
  caption,
  canTrigger,
  hasReport,
  expanded,
  onToggle,
  onTrigger,
  triggering,
  report,
}: PixelCaptainDockProps) {
  // 每进入一次 COMPLETED 播一次 Judge 动作：`judgeAt` 是那一次的相位序号。
  // 用它做 sprite 的 key，React 会重建动画节点 —— 不需要计时器，也不需要
  // 手动 add/remove class，因此不会出现「动画被外部打断后卡在中间帧」。
  const [judgeAt, setJudgeAt] = useState(0)
  const prevPhase = useRef<PixelCaptainPhase>(phase)
  useEffect(() => {
    if (prevPhase.current !== 'COMPLETED' && phase === 'COMPLETED') {
      setJudgeAt((n) => n + 1)
    }
    prevPhase.current = phase
  }, [phase])

  const time = timeText(report?.updated_at ?? report?.created_at ?? null)
  // 只有判过才显示「判过的时刻」，其余状态不显示一个无关时间戳。
  const showTime = phase === 'COMPLETED' && Boolean(time)

  return (
    <div className="pixel-captain" data-phase={phase}>
      <PixelCaptain phase={phase} judgeKey={judgeAt} />

      <div className="pixel-captain__say">
        <p className="pixel-captain__bubble" role="status" aria-live="polite">
          {caption}
        </p>

        <div className="pixel-captain__actions">
          {phase === 'COMPLETED' && hasReport ? (
            <button
              type="button"
              className="pixel-captain__action"
              onClick={onToggle}
              aria-expanded={expanded}
            >
              {expanded ? `收起${CAPTAIN_LABEL}报告` : `查看${CAPTAIN_LABEL}报告`}
            </button>
          ) : null}

          {(phase === 'IDLE' || phase === 'FAILED') && canTrigger && onTrigger ? (
            <button
              type="button"
              className="pixel-captain__action pixel-captain__action--quiet"
              disabled={triggering}
              onClick={onTrigger}
            >
              {phase === 'FAILED' ? '再试一次' : '请求分析'}
            </button>
          ) : null}

          <span className="pixel-captain__meta">
            {CAPTAIN_LABEL}
            {showTime ? <time className="pixel-captain__time">{time}</time> : null}
          </span>
        </div>
      </div>
    </div>
  )
}
