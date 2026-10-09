/**
 * 演示输入 / 水华队长的**展示层**常量。
 *
 * 这里**刻意没有唤醒词**。唤醒词的唯一所有者是后端
 * （`CAPTAIN_WAKE_WORDS` → `app/captain/wake_word.py`）；前端若也存一份，
 * 部署改了措辞后按钮就会发一句唤不醒的话，而且失败是静默的（没有报错，
 * 只是裁判不出现）。所以快捷发送走 `trigger_captain: true`，由服务端用它
 * 自己的配置组句 —— 前端连唤醒词是什么都不需要知道。
 */

/** 演示输入条的按钮文案（纯 UI 标签，不参与任何匹配逻辑）。 */
export const CAPTAIN_LABEL = '水华队长'

export interface DemoSpeaker {
  name: string
  /**
   * 该角色的识别色（CSS 变量名，值在 global.css 里，来自 designTokens）。
   * 用**已有的**调色板，不新造颜色 —— 视觉体系不能因为一个演示条而发散。
   */
  color: string
}

/**
 * 三个预设人设。数量固定为 3 是有意的：
 *  - 少于 3 撑不起「多方讨论」的演示价值（也测不到多观点裁判）；
 *  - 多于 3 会让输入条在会议室页底部变得笨重。
 *
 * 这些**只是演示用的发言标签**，不是 speaker attribution。真正的说话人身份在
 * 本系统中不可靠（ASR_SPEAKER_LABELS_ENABLED=false 时 speaker_id 恒为 -1），
 * 所以 ADR-002 明确禁止裁判报告承载人名/职位 —— UI 上的标签不会、也不能
 * 反向变成裁判的归因依据。
 */
export const DEMO_SPEAKERS: readonly DemoSpeaker[] = [
  { name: '主持人', color: 'var(--color-primary)' },
  { name: '产品', color: 'var(--color-mint)' },
  { name: '技术', color: 'var(--color-warning)' },
] as const
