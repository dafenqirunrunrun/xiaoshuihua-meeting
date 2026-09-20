/**
 * Transcript Evidence 定位契约（Step UI-INTEL-2A）。
 *
 * 唯一依据是服务端给出的 `source_segment_seqs` —— AI 结论 → segment_seq →
 * Transcript 行。前端**只负责定位与展示**，绝不：
 *   - 关键词搜索 / 文本模糊匹配（那是把展示层的猜测伪装成证据）
 *   - 按 task / text 文案猜对应转写行
 *   - 重新调用 LLM 判断依据
 *
 * 这里只做「合法化」：去重、升序、丢弃非正整数、限流。任何来源（未来的
 * route query 也走这里）都必须过一遍，不直接信任输入。
 */

/** 单次导航最多接受多少个 seq；超出截断（URL / 服务端输入都不无条件信任）。 */
export const EVIDENCE_MAX_SEQS = 50

/**
 * Transcript FINAL 行的稳定 DOM 标识（唯一业务标识）。
 *
 * 用 `segment_seq` —— DB 上 `UNIQUE(meeting_id, segment_seq)`，是全局稳定业务键。
 * **禁止**改用数组 index / 文本内容 / utterance_id：前两者会随列表变化漂移，
 * utterance_id 是 session 内派生的，不是定位契约。
 */
export const EVIDENCE_SEGMENT_ATTR = 'data-segment-seq'

/** 某条 evidence 的 DOM 选择器。属性名只在此处定义，避免散落硬编码。 */
export function evidenceSelector(segmentSeq: number): string {
  return `[${EVIDENCE_SEGMENT_ATTR}="${segmentSeq}"]`
}

/**
 * 规范化 evidence seq 列表：仅保留正整数 → 去重 → 升序 → 限流。
 *
 * 返回空数组是合法结果（调用方据此**不渲染入口**，而不是渲染一个 disabled 假按钮）。
 */
export function normalizeEvidenceSeqs(
  raw: readonly number[] | null | undefined,
): number[] {
  if (!raw || raw.length === 0) return []
  const seen = new Set<number>()
  for (const value of raw) {
    if (typeof value !== 'number' || !Number.isInteger(value) || value <= 0) continue
    seen.add(value)
    if (seen.size >= EVIDENCE_MAX_SEQS) break
  }
  return [...seen].sort((a, b) => a - b)
}
