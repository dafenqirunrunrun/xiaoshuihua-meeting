import type { ReactNode } from 'react'

interface EmptyPlaceholderProps {
  icon?: ReactNode
  title?: string
  description?: string
  action?: ReactNode
  compact?: boolean
}

/**
 * 轻量空态（替代默认巨型 Ant Empty）：
 * 小图标 + 标题 + 说明 + 可选动作，像素级收敛高度，避免白屏式空白。
 */
export function EmptyPlaceholder({
  icon,
  title,
  description,
  action,
  compact,
}: EmptyPlaceholderProps) {
  return (
    <div className={`empty-state${compact ? ' empty-state--compact' : ''}`}>
      {icon ? <div className="empty-state__icon">{icon}</div> : null}
      {title ? <div className="empty-state__title">{title}</div> : null}
      {description ? <div className="empty-state__desc">{description}</div> : null}
      {action ? <div className="empty-state__action">{action}</div> : null}
    </div>
  )
}
