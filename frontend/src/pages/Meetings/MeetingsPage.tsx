import { useMemo, useState } from 'react'
import { Button, Skeleton, Tabs, Typography } from 'antd'
import { CalendarOutlined, ReloadOutlined } from '@ant-design/icons'
import { EmptyPlaceholder } from '@/components/EmptyPlaceholder'
import { MeetingListItem } from '@/components/MeetingListItem'
import { useMeetingList } from '@/hooks/useMeetingList'
import type { MeetingStatus } from '@/types/meeting'

const { Title } = Typography

/** 列表页分页：每页 20，后端默认亦为 20（LIST_DEFAULT_LIMIT）。 */
const PAGE_LIMIT = 20

/**
 * 会议列表页（Step 8B-6：placeholder 升级为真实列表）。
 * 状态 Tabs → GET /api/meetings?status=...（真实后端 status filter）。
 * 每条（MeetingListItem）：title / 状态 Tag / 创建或开始时间 / 真实时长 / 状态 CTA。
 * 无 fake 数据；空态 / loading / error 均轻量。
 */
export function MeetingsPage() {
  // Tab 键 → status 过滤；「全部」不传 status
  type TabKey = 'all' | MeetingStatus
  const [tab, setTab] = useState<TabKey>('all')

  const query = useMemo(
    () => (tab === 'all' ? { limit: PAGE_LIMIT, offset: 0 } : { status: tab, limit: PAGE_LIMIT, offset: 0 }),
    [tab],
  )
  const { meetings, total, loading, error, reload } = useMeetingList(query)

  return (
    <div className="page">
      <div className="section-header" style={{ marginBottom: 20 }}>
        <div>
          <Title level={3} className="h-m0">
            会议
          </Title>
          <div className="section-subtitle">管理你的全部会议记录。</div>
        </div>
      </div>

      <div className="panel meetings-panel" style={{ padding: '0 20px 20px' }}>
        <Tabs
          activeKey={tab}
          onChange={(k) => setTab(k as TabKey)}
          items={[
            { key: 'all', label: '全部' },
            { key: 'CREATED', label: '待开始' },
            { key: 'IN_PROGRESS', label: '进行中' },
            { key: 'FINALIZING', label: '收尾中' },
            { key: 'COMPLETED', label: '已完成' },
          ]}
        />

        {loading ? (
          <div style={{ padding: '12px 4px 24px' }}>
            <Skeleton active paragraph={{ rows: 3 }} title={false} />
          </div>
        ) : null}

        {!loading && error ? (
          <div style={{ padding: '12px 4px' }}>
            <EmptyPlaceholder
              icon={<ReloadOutlined />}
              title="会议列表加载失败"
              description={error}
              action={
                <Button size="small" icon={<ReloadOutlined />} onClick={reload}>
                  重试
                </Button>
              }
              compact
            />
          </div>
        ) : null}

        {!loading && !error && meetings.length === 0 ? (
          <div style={{ padding: '12px 4px' }}>
            <EmptyPlaceholder
              icon={<CalendarOutlined />}
              title="还没有会议记录"
              description="创建会议后，实时转写与 AI 纪要将在这里沉淀。"
              compact
            />
          </div>
        ) : null}

        {!loading && !error && meetings.length > 0 ? (
          <>
            {meetings.map((m) => (
              <MeetingListItem key={m.id} meeting={m} />
            ))}
            {meetings.length < total ? (
              <div className="meetings-more">
                已显示 {meetings.length} / {total} 条，更多记录请在后续版本翻页查看。
              </div>
            ) : null}
          </>
        ) : null}
      </div>
    </div>
  )
}
