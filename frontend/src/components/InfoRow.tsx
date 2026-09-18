import type { ReactNode } from 'react'

interface InfoRowProps {
  label: string
  value: ReactNode
}

/** 会议信息面板的 标签 / 值 行 */
export function InfoRow({ label, value }: InfoRowProps) {
  return (
    <div className="info-row">
      <span className="info-row__label">{label}</span>
      <span className="info-row__value">{value}</span>
    </div>
  )
}
