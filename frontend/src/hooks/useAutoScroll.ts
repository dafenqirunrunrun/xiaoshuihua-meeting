import { useEffect, useRef, useState, type RefObject } from 'react'

/**
 * 实时字幕自动滚动（Step 8B-4）。
 *
 * 规则：
 *   - 用户处于底部附近（distanceToBottom <= 100px）→ 新内容自动滚到底部
 *     （含 PARTIAL 文本原地变长：contentKey 变化即检测）。
 *   - 用户主动向上查看历史 → autoFollow false，新字幕进 store 但不抢滚动，
 *     显示「有新内容 · 回到底部」轻量按钮；点击后 jumpToBottom + 恢复跟随。
 *   - 首次进入已有 Transcript 的页面：Snapshot 渲染后 instant 滚到底部（不 smooth）。
 *   - 不在底部时，PARTIAL 每次更新不抢滚动（只显示回到底部提示）。
 *
 * UI-INTEL-2A：新增 `pauseAutoFollow()`，供「查看原文」evidence 跳转使用。
 *
 *   **不另造第二套暂停状态** —— evidence 定位与「用户手动向上滚」共用同一个
 *   `autoFollowRef`。原因：两者对跟随的意图完全相同（「用户正在看别处，别抢
 *   滚动」），分成两份状态就会出现两套互相打架的暂停语义：
 *   例如 evidence 跳转期间来了新 FINAL，若 auto-scroll 只看自己的 ref 就会把
 *   用户拽回底部（§10 明确禁止）。
 *
 *   调用方（evidence 导航）**必须先 pause 再 scroll**：先落状态，后滚动，
 *   避免 smooth 滚动途中恰好有新内容到达而被抢走。
 *
 *   恢复跟随的唯一入口仍是用户显式点击 FAB（`jumpToBottom`）——
 *   evidence 跳转本身绝不自动恢复跟随。
 *
 * 纯 UI 行为，不影响任何数据语义（SSE 永远不是事实源）。
 */

/** 距底部多少 px 内视为「在底部」；宽松判定，不强求绝对相等 */
const BOTTOM_THRESHOLD = 100

export function useAutoScroll(
  contentRef: RefObject<HTMLElement>,
  contentKey: string,
) {
  const [showJumpButton, setShowJumpButton] = useState(false)
  const [atBottom, setAtBottom] = useState(true)
  const autoFollowRef = useRef(true)
  const prevKeyRef = useRef(contentKey)
  const firstPaintRef = useRef(true)

  const scrollBottom = (smooth: boolean) => {
    const el = contentRef.current
    if (!el) return
    if (smooth) {
      el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
    } else {
      el.scrollTop = el.scrollHeight
    }
  }

  const jumpToBottom = () => {
    scrollBottom(true)
    autoFollowRef.current = true
    setShowJumpButton(false)
    setAtBottom(true)
  }

  /**
   * 暂停 auto-follow（UI-INTEL-2A：evidence 跳转前调用）。
   *
   * 只动**同一个** `autoFollowRef`：置 false 后新内容不会再抢滚动，
   * FAB 亮起提示「回到最新」；用户点 FAB 即走既有 `jumpToBottom` 恢复跟随。
   *
   * **它并非总是唯一的暂停来源**（E2E 2A-25 记录的事实）：`onScroll` 会按几何
   * 位置重新推导 `autoFollowRef`，所以一次**真的滚动了**的 evidence 跳转，
   * 滚动后 onScroll 自己就会把跟随关掉。此时本调用与几何推导结论一致，
   * 属冗余但无害。
   *
   * 真正**只有本调用能覆盖**的是「跳转目标已在视口内 → 滚动量为 0」：没有
   * `scroll` 事件 → onScroll 不跑 → 若没有本调用，下一条 FINAL 会把用户从
   * 证据处直接拽回底部。所以这里必须**显式** pause，不能依赖几何副作用
   * （那是在依赖一个和意图无关的实现细节）。
   */
  const pauseAutoFollow = () => {
    autoFollowRef.current = false
    setShowJumpButton(true)
  }

  const onScroll = () => {
    const el = contentRef.current
    if (!el) return
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight
    const nearBottom = distance <= BOTTOM_THRESHOLD
    setAtBottom(nearBottom)
    if (nearBottom) {
      autoFollowRef.current = true
      setShowJumpButton(false)
    } else {
      autoFollowRef.current = false
      setShowJumpButton(true)
    }
  }

  // contentKey 变化（新 FINAL / 新 PARTIAL 行 / PARTIAL 文本变长）→ 按跟随状态处理
  useEffect(() => {
    if (contentKey === prevKeyRef.current) return
    prevKeyRef.current = contentKey

    if (autoFollowRef.current) {
      // 首次内容出现：instant；后续：smooth
      scrollBottom(!firstPaintRef.current)
      firstPaintRef.current = false
      setShowJumpButton(false)
      setAtBottom(true)
    } else {
      // 用户在看历史：不抢滚动，给「回到底部」提示
      setShowJumpButton(true)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [contentKey])

  // 滚动监听：被动，绝不阻断
  useEffect(() => {
    const el = contentRef.current
    if (!el) return
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [contentRef.current])

  return { showJumpButton, atBottom, jumpToBottom, pauseAutoFollow }
}
