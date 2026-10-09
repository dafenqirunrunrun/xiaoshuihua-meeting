import { http } from './http'

/**
 * 演示输入（会议室页「模拟参会人发言」）。
 *
 * 后端 `POST /api/meetings/:id/demo/utterances` 把一条发言变成**真实 FINAL**，
 * 走的是与真实 ASR 完全相同的 ingress 应用层 —— 所以 ordering / SSE /
 * Meeting State / 水华队长唤醒全部一致，不存在「演示专用链路」。
 *
 * ⚠️ 请求体里**没有** `segment_seq`：序号由 Transcript Domain 分配
 * （CLAUDE.md 硬规则「segment_seq 由 Backend 生成」）。回传的才是事实源，
 * 前端渲染一律用返回值，绝不自行推算。
 */

export interface DemoUtteranceIn {
  /** 演示用发言人标识（如「主持人」）。仅映射为不透明 speakerId，不承载身份语义。 */
  speaker: string
  /** 发言内容。`trigger_captain` 为 true 时可省略（文本由服务端唤醒词填入）。 */
  text?: string
  /**
   * 快捷发送：让**服务端**以它自己配置的唤醒词作为发言内容。
   *
   * 前端刻意不持有唤醒词 —— 硬编码会在部署改 `CAPTAIN_WAKE_WORDS` 后
   * 发一句唤不醒的话，且失败是静默的（无报错，只是裁判不出现）。
   */
  trigger_captain?: boolean
  /** 可选：显式起点。省略时服务端顺序推进，保证时间轴单调。 */
  start_ms?: number
  duration_ms?: number
}

export interface DemoUtteranceAccepted {
  meeting_id: string
  /** 服务端分配的序号 —— 前端只能被告知，不能指定。 */
  segment_seq: number
  utterance_id: string
  speaker: string
  speaker_id: number
  start_ms: number
  end_ms: number
  text: string
  /**
   * 服务端判定这句话是否请求了水华队长。
   * 唤醒规则只有一处实现（后端 `captain/wake_word.py`）——前端**绝不**自行匹配，
   * 否则两份实现必然漂移，而漏判是静默的。
   */
  triggered_captain: boolean
}

export async function sendDemoUtterance(
  meetingId: string,
  payload: DemoUtteranceIn,
): Promise<DemoUtteranceAccepted> {
  const { data } = await http.post<DemoUtteranceAccepted>(
    `/api/meetings/${meetingId}/demo/utterances`,
    payload,
  )
  return data
}
