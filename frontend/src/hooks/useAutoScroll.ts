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

  return { showJumpButton, atBottom, jumpToBottom }
}
