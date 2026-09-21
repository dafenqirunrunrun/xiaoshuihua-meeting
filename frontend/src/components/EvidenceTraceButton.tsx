import { Button } from 'antd'
import { SearchOutlined } from '@ant-design/icons'
import { normalizeEvidenceSeqs } from '@/utils/evidence'

/**
 * 「查看原文 / 查看依据」入口（Step UI-INTEL-2A）。
 *
 * 两条硬约束：
 *  1. 必须是**真 button**（§17）。用 AntD Button —— 键盘可达、有正确的
 *     role / focus 语义。**禁止** `<div onClick>` / `<span onClick>`：
 *     那样屏幕阅读器与键盘用户都拿不到这个入口。
 *  2. **没有 evidence 就不渲染**（§6）。不是 disabled 假按钮 —— 一个点不动
 *     的按钮只会让人以为功能坏了，而不是"这条本来就没有依据"。
 *
 * 本组件不做定位：点击只把 `source_segment_seqs` 交给调用方。定位逻辑收在
 * `useTranscriptEvidenceNavigation`（§8），不在这里散落 querySelector。
 */
export function EvidenceTraceButton({
  sourceSegmentSeqs,
  onTrace,
  label = '查看原文',
}: {
  sourceSegmentSeqs: readonly number[] | null | undefined
  onTrace: (seqs: number[]) => void
  label?: string
}) {
  const seqs = normalizeEvidenceSeqs(sourceSegmentSeqs)
  if (seqs.length === 0) return null

  return (
    <Button
      type="link"
      size="small"
      className="intel-trace"
      icon={<SearchOutlined />}
      onClick={() => onTrace(seqs)}
    >
      {label}
    </Button>
  )
}
