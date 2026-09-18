import type { SseEnvelope, SseEventType } from '@/types/sse'

/**
 * 后端发出的**具名** SSE 帧的事件名清单（与 `app/realtime/schemas.py` 的
 * SSE_EVENT_TYPES 一一对应）。
 *
 * 关键：SSE 规范里 `event: <name>` 的帧**不会**触发 `onmessage` ——
 * 只有**未具名**帧才会。后端 `_render_frame` 每一帧都写 `event: {event_type}`
 * （契约见 docs/api-contract.md L194-195），所以 `addEventListener('message')`
 * 永远不会收到任何业务事件。必须逐事件名订阅。
 */
export const SSE_EVENT_TYPES: readonly SseEventType[] = [
  'transcript.partial',
  'transcript.final',
  'meeting.status',
  'summary.started',
  'summary.completed',
  'summary.failed',
  'heartbeat',
] as const

/**
 * 为一个 EventSource 订阅所有已知具名事件，统一解析为 `SseEnvelope` 后回调。
 *
 * - 具名事件（后端实际发送形式）→ 逐名 addEventListener；
 * - 兼容 `message`（未具名帧）→ 一并订阅，保证协议两端任一形式都能工作；
 * - 畸形帧忽略，绝不打断流；
 * - 返回清理函数（close 时移除，避免悬挂引用）。
 *
 * 调用方负责 gen/meeting-id 等防御性校验。
 */
export function subscribeSseEnvelopes(
  es: EventSource,
  onEnvelope: (env: SseEnvelope) => void,
): () => void {
  const handler = (e: Event) => {
    const data = (e as MessageEvent).data
    if (typeof data !== 'string') return
    try {
      const env = JSON.parse(data) as SseEnvelope
      if (!env || typeof env.event_type !== 'string') return
      onEnvelope(env)
    } catch {
      /* 畸形帧：忽略，不打断流 */
    }
  }

  for (const type of SSE_EVENT_TYPES) {
    es.addEventListener(type, handler)
  }
  // 兼容未具名帧（后端当前不发，但协议上 message 是默认事件名）
  es.addEventListener('message', handler)

  return () => {
    for (const type of SSE_EVENT_TYPES) {
      es.removeEventListener(type, handler)
    }
    es.removeEventListener('message', handler)
  }
}
