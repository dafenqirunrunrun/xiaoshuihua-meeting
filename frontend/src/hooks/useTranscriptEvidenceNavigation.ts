import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'
import {
  EVIDENCE_SEGMENT_ATTR,
  evidenceSelector,
  normalizeEvidenceSeqs,
} from '@/utils/evidence'

/**
 * Transcript Evidence Navigation（Step UI-INTEL-2A）。
 *
 * 「查看原文」的**唯一**定位能力。所有 DOM 查询都收在这里 —— 不在 Decision /
 * Action / Risk 每个组件里复制 `querySelector + scrollIntoView`（§8）。
 *
 * 职责边界（严格）：
 *   - 输入 `source_segment_seqs`（规范化后）→ 找到对应 FINAL 行 → 滚动 → 高亮。
 *   - **不推导**：不做关键词搜索 / 模糊匹配 / 重新调用 LLM（§5）。
 *   - **不改数据**：只动 `class` 与滚动位置，绝不触碰 store / 后端。
 *
 * 定位依据只有一个：`data-segment-seq`（见 utils/evidence.ts）。**禁止**用
 * 数组 index / 文本内容 / utterance_id 定位（§4）—— 那些都会漂移。
 *
 * 关键行为：
 *   - 多 evidence：滚到**最小**有效 seq，**所有实际存在**的 seq 同时高亮（§9）；
 *     部分 seq 不存在 → 忽略不存在的，只有**全部**找不到才提示不可定位（§6）。
 *   - 连续点击同一条：每次都重新定位 + 重新触发高亮。靠**重排强制重启**
 *     （class 移除 → 读 offsetWidth → 重新加），而不是靠 state 比较 ——
 *     state 值相同 React 会 no-op，第二次点击就"没反应"了（§12）。
 *   - 快速切换：单 timer，切目标先 clearTimeout，最后的目标胜出（§13）。
 *   - Snapshot 未就绪：不报错，挂起为 loading 提示，Snapshot 到位后自动补一次
 *     定位，不需要再发一次 Transcript 请求（§14）。
 *   - prefers-reduced-motion：走 instant 滚动（§11）。注意全局 CSS 的
 *     `scroll-behavior: auto !important` **管不到** 显式传入的 behavior 参数，
 *     所以必须在这里显式判断，否则 reduced-motion 用户仍会被平滑滚动。
 *
 * 与 auto-scroll 的协作（§10，本步重点）：跳转前由调用方先 `pauseAutoFollow()`，
 * 本 hook 不持有跟随状态 —— 暂停语义只有一份，避免两套状态互相打架。
 */

/** 高亮持续时间（§11：1.5~3s）。 */
const HIGHLIGHT_MS = 2400

/** 高亮 class（样式见 global.css，仅 border/background 轻量变化）。 */
const HIGHLIGHT_CLASS = 'transcript-segment--evidence'

/** 不可定位提示的存活时长：与高亮同步，不弹窗、不阻塞（§6）。 */
const NOTICE_MS = HIGHLIGHT_MS

export interface EvidenceNavigationState {
  /** 上次导航是否定位成功；'idle' 表示还没点过（不渲染任何提示） */
  outcome: 'idle' | 'not_found' | 'loading'
  /** 实际锚定的 segment_seq（min 有效）——仅用于 aria-live 文案，不是数据源 */
  anchorSeq: number | null
  /**
   * 执行一次 evidence 定位。
   *
   * seqs 为空 / 全部非法 → 直接返回（入口本就不该渲染，§6）。
   * 找不到且 Snapshot 仍在加载 → 挂起为 'loading'，加载完成后自动补做。
   */
  traceEvidence: (raw: readonly number[] | null | undefined) => void
}

function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined' || !window.matchMedia) return false
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

/** 高亮一条 seq 对应的行（若存在）。返回是否命中。 */
function highlightOne(container: HTMLElement, seq: number): boolean {
  const el = container.querySelector<HTMLElement>(evidenceSelector(seq))
  if (!el) return false
  if (el.classList.contains(HIGHLIGHT_CLASS)) {
    // 重排强制重启动画：同一条连续点击必须重新触发（§12）
    el.classList.remove(HIGHLIGHT_CLASS)
    void el.offsetWidth
  }
  el.classList.add(HIGHLIGHT_CLASS)
  el.setAttribute('data-evidence-active', 'true')
  return true
}

function clearHighlight(container: HTMLElement | null, seqs: readonly number[]): void {
  if (!container) return
  for (const seq of seqs) {
    const el = container.querySelector<HTMLElement>(evidenceSelector(seq))
    if (!el) continue
    el.classList.remove(HIGHLIGHT_CLASS)
    el.removeAttribute('data-evidence-active')
  }
}

/**
 * 把 `target` 滚到滚动容器中部。
 *
 * 手算偏移而不是 `scrollIntoView({block:'center'})`：后者会把**所有**可滚动祖先
 * 一起滚，包括页面本身 —— 在会议室里会把整页往上推。这里只动转写列表这个容器，
 * 页面位置不变（§9 允许按兼容策略调整具体做法）。
 */
function scrollIntoCenter(
  container: HTMLElement,
  target: HTMLElement,
  smooth: boolean,
): void {
  const targetRect = target.getBoundingClientRect()
  const containerRect = container.getBoundingClientRect()
  const top =
    container.scrollTop +
    (targetRect.top - containerRect.top) -
    (container.clientHeight - targetRect.height) / 2
  container.scrollTo({
    top: Math.max(0, top),
    behavior: smooth ? 'smooth' : 'auto',
  })
}

export function useTranscriptEvidenceNavigation(
  containerRef: RefObject<HTMLElement>,
  /**
   * 转写内容是否已「定稿」—— 即 Snapshot 加载已结束（成功或失败都算）。
   *
   * 用来区分两种「找不到容器」：
   *   - false（还在加载）→ 挂起为 'loading'，Snapshot 到位后自动补做定位（§14）；
   *   - true（已定稿仍找不到）→ 判定为不可定位，给轻提示。
   * 少了这个信号，空转写 / 加载失败会让提示**永久停在「正在加载」**——
   * 那是在撒谎说"马上就好"。
   */
  contentSettled: boolean,
): EvidenceNavigationState {
  const [outcome, setOutcome] = useState<'idle' | 'not_found' | 'loading'>('idle')
  const [anchorSeq, setAnchorSeq] = useState<number | null>(null)

  /** 当前高亮的 seq（用于下一次导航 / 卸载时清理） */
  const highlightedRef = useRef<number[]>([])
  const highlightTimerRef = useRef<number | null>(null)
  const noticeTimerRef = useRef<number | null>(null)
  /** 挂起的一次导航：等待 Snapshot 到位后补做（§14） */
  const pendingRef = useRef<number[] | null>(null)
  /** effect 里读最新值，避免把 contentSettled 塞进依赖导致重复补做 */
  const contentSettledRef = useRef(contentSettled)
  contentSettledRef.current = contentSettled

  const clearTimers = useCallback(() => {
    if (highlightTimerRef.current !== null) {
      window.clearTimeout(highlightTimerRef.current)
      highlightTimerRef.current = null
    }
    if (noticeTimerRef.current !== null) {
      window.clearTimeout(noticeTimerRef.current)
      noticeTimerRef.current = null
    }
  }, [])

  /** 进入「不可定位」轻提示态：定时自动回到 idle，不弹窗、不阻塞（§6）。 */
  const enterNotFound = useCallback(() => {
    highlightedRef.current = []
    pendingRef.current = null
    setAnchorSeq(null)
    setOutcome('not_found')
    if (noticeTimerRef.current !== null) window.clearTimeout(noticeTimerRef.current)
    noticeTimerRef.current = window.setTimeout(() => {
      noticeTimerRef.current = null
      setOutcome('idle')
    }, NOTICE_MS)
  }, [])

  /** 真正执行定位。'not_ready' 表示容器还没渲染出来（可能只是还没加载完）。 */
  const attemptNavigation = useCallback(
    (seqs: number[]): 'done' | 'not_ready' => {
      const container = containerRef.current
      if (!container) return 'not_ready'

      // 先摘掉上一次的高亮 —— 快速切换时旧高亮不残留（§13）
      clearHighlight(container, highlightedRef.current)
      clearTimers()

      const present = seqs.filter((seq) =>
        container.querySelector(evidenceSelector(seq)) !== null,
      )

      if (present.length === 0) {
        // 一个都找不到：轻提示，绝不弹窗报错，也不影响其它功能（§6）
        enterNotFound()
        return 'done'
      }

      // 滚到最小有效 seq（§9），所有存在的 evidence 同时高亮
      const anchor = present[0]
      const anchorEl = container.querySelector<HTMLElement>(evidenceSelector(anchor))
      if (anchorEl) {
        scrollIntoCenter(container, anchorEl, !prefersReducedMotion())
      }
      for (const seq of present) highlightOne(container, seq)

      highlightedRef.current = present
      pendingRef.current = null
      setAnchorSeq(anchor)
      setOutcome('idle')

      highlightTimerRef.current = window.setTimeout(() => {
        highlightTimerRef.current = null
        clearHighlight(containerRef.current, highlightedRef.current)
        highlightedRef.current = []
        setAnchorSeq(null)
      }, HIGHLIGHT_MS)

      return 'done'
    },
    [clearTimers, containerRef, enterNotFound],
  )

  const traceEvidence = useCallback(
    (raw: readonly number[] | null | undefined) => {
      const seqs = normalizeEvidenceSeqs(raw)
      // 空 evidence：入口不该渲染。真被调用也只是一次静默 no-op，不是错误。
      if (seqs.length === 0) return
      pendingRef.current = seqs
      if (attemptNavigation(seqs) === 'done') return
      // 容器还没渲染：加载中 → 挂起等补做；已定稿 → 就是不可定位
      if (contentSettledRef.current) enterNotFound()
      else setOutcome('loading')
    },
    [attemptNavigation, enterNotFound],
  )

  // Snapshot 定稿后补做挂起的那一次定位（不额外发 Transcript 请求）。
  // 无依赖数组：每次 render 都检查一次，这就是「等内容就绪」的实现方式。
  useEffect(() => {
    if (outcome !== 'loading') return
    const pending = pendingRef.current
    if (!pending) {
      setOutcome('idle')
      return
    }
    if (attemptNavigation(pending) === 'done') return
    if (contentSettledRef.current) enterNotFound()
  })

  // 卸载 / 切换会议：清 timer + 摘高亮，不留悬挂状态
  useEffect(() => {
    return () => {
      clearTimers()
      clearHighlight(containerRef.current, highlightedRef.current)
      highlightedRef.current = []
      pendingRef.current = null
    }
  }, [clearTimers, containerRef])

  return { outcome, anchorSeq, traceEvidence }
}

export { EVIDENCE_SEGMENT_ATTR, HIGHLIGHT_MS }
