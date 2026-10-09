import type { PixelCaptainState } from '@/hooks/usePixelCaptainState'

/**
 * 像素小人本体（Phase 3-1）。
 *
 * 这里**只有画**：没有状态、没有请求、没有计时器。输入是一个已经由
 * `usePixelCaptainState` 从真实 captain 字段派生好的 phase，输出是几块像素。
 * 换掉这个文件不会影响任何一条数据流 —— 这是刻意的：角色是表现层，不是数据层。
 *
 * 为什么不用 sprite sheet：第一版只需要「能认出这是同一个角色在不同状态下」。
 * 纯 CSS 像素块 + keyframes 能做到，且零新依赖（Phase 3-1 明确不引入动画库）。
 * 后续若要换成真 sprite sheet，只需改这一个文件。
 */

export type PixelCaptainPhase = PixelCaptainState

/**
 * 每个相位对应的**可访问名**。
 *
 * 小人本身 `aria-hidden`：状态语义已经由 dock 上的 `role="status"` 气泡用文字
 * 播报（「正在回顾刚才的讨论…」比「像素小人天线闪烁」对读屏用户更有意义）。
 * 这里保留一份映射，供将来需要独立播报时使用，同时把「每个状态都必须有名字」
 * 变成类型上的义务 —— 新增相位时 TS 会在这里报错。
 */
export const PIXEL_CAPTAIN_PHASE_LABEL: Record<PixelCaptainPhase, string> = {
  IDLE: '待机中',
  TRIGGERED: '已被唤起',
  ANALYZING: '分析中',
  COMPLETED: '分析完成',
  FAILED: '未能完成',
}

interface PixelCaptainProps {
  phase: PixelCaptainPhase
  /**
   * Judge 动作的相位序号，由 dock 在每次「进入 COMPLETED」时 +1。
   *
   * 用它当 `key` 会重建动画节点，从而让 `pixel-captain-judge` 从头再播一次。
   * 这样就不需要任何计时器，也不需要手动 add/remove class —— 也就不会出现
   * 「动画中途被外部打断后卡在中间帧」这种 classic 竞态。
   */
  judgeKey?: number
}

export function PixelCaptain({ phase, judgeKey = 0 }: PixelCaptainProps) {
  return (
    <div className="pixel-captain__sprite" data-phase={phase} aria-hidden>
      {/* 地面投影：让人物「站」在页面里，而不是浮着 */}
      <span className="pixel-captain__ground" />

      <span className="pixel-captain__bob" key={judgeKey}>
        {/* 天线 + 指示灯：ANALYZING 时闪，其余状态常亮 */}
        <span className="pixel-captain__antenna">
          <span className="pixel-captain__rod" />
          <span className="pixel-captain__lamp" />
        </span>

        {/* 面罩 + 两只眼睛 */}
        <span className="pixel-captain__visor">
          <span className="pixel-captain__eye" />
          <span className="pixel-captain__eye pixel-captain__eye--right" />
        </span>

        {/* 像素小锤：只在 COMPLETED 的 Judge 动作里第一次真正出现 */}
        <span className="pixel-captain__gavel" />

        <span className="pixel-captain__torso" />
        <span className="pixel-captain__base" />
      </span>
    </div>
  )
}
