import { Card, Tabs, Typography } from 'antd'
import { FileTextOutlined } from '@ant-design/icons'
import { EmptyPlaceholder } from '@/components/EmptyPlaceholder'

const { Title } = Typography

/**
 * Meeting Result —— Step 8A.1 视觉落地 + 8B-5 骨架打磨。
 * 双 Tab（会议纪要 / 会议内容）保留；纪要 tab 建立 Overview / Topics /
 * Conclusions / Open Questions 四区信息层级（8C 用真实数据填充）。
 */
export function MeetingResultPage() {
  return (
    <div className="page result">
      <div className="result-head">
        <Title level={1} className="result-head__title h-m0">
          会议结果
        </Title>
        <div className="result-head__meta">
          <span>会议纪要</span>
          <span aria-hidden>·</span>
          <span>时长待计算</span>
          <span aria-hidden>·</span>
          <span>已完成</span>
        </div>
      </div>

      <Card
        className="result-tabs"
        style={{ borderRadius: 16, borderColor: '#E8EEF5' }}
        styles={{ body: { padding: '4px 24px 24px' } }}
      >
        <Tabs
          defaultActiveKey="summary"
          items={[
            {
              key: 'summary',
              label: '会议纪要',
              children: (
                <div className="summary">
                  {/* 会议概述 */}
                  <div className="summary__block">
                    <div className="summary__label">会议概述</div>
                    <p className="summary__placeholder">
                      AI 纪要将在会议结束后在此生成。
                    </p>
                  </div>
                  {/* 核心议题 */}
                  <div className="summary__block">
                    <div className="summary__label">核心议题</div>
                    <p className="summary__placeholder">
                      暂无议题 —— 待纪要生成后列出本次会议的核心话题。
                    </p>
                  </div>
                  {/* 主要结论 */}
                  <div className="summary__block">
                    <div className="summary__label">主要结论</div>
                    <p className="summary__placeholder">
                      暂无结论 —— 待纪要生成后汇总本场达成的共识。
                    </p>
                  </div>
                  {/* 未决问题 */}
                  <div className="summary__block">
                    <div className="summary__label">未决问题</div>
                    <p className="summary__placeholder">
                      暂无明确未决问题
                    </p>
                  </div>
                </div>
              ),
            },
            {
              key: 'transcript',
              label: '会议内容',
              children: (
                <div style={{ paddingTop: 12 }}>
                  <EmptyPlaceholder
                    icon={<FileTextOutlined />}
                    title="暂无会议内容"
                    description="完整 FINAL Transcript 将在这里按时间顺序展示。"
                    compact
                  />
                </div>
              ),
            },
          ]}
        />
      </Card>
    </div>
  )
}
