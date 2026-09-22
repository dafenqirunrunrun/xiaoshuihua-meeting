import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  Button,
  DatePicker,
  Input,
  Modal,
  Pagination,
  Select,
  Skeleton,
  Tabs,
  Typography,
  message,
} from 'antd'
import {
  AudioOutlined,
  CalendarOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  HomeOutlined,
  Loading3QuartersOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import { EmptyPlaceholder } from '@/components/EmptyPlaceholder'
import { MeetingListItem } from '@/components/MeetingListItem'
import { useMeetingList } from '@/hooks/useMeetingList'
import { deleteMeeting, normalizeApiError, updateMeeting } from '@/api'
import type { Meeting, MeetingListSort, MeetingStatus } from '@/types/meeting'

const { Title } = Typography

/** 桌面列表保持适宜浏览密度，页码通过 URL 保存。 */
const PAGE_LIMIT = 8
type TabKey = 'all' | MeetingStatus
const TAB_KEYS: TabKey[] = ['all', 'CREATED', 'IN_PROGRESS', 'FINALIZING', 'COMPLETED']
const SORT_KEYS: MeetingListSort[] = ['updated_desc', 'created_desc', 'created_asc']

function isTabKey(value: string | null): value is TabKey {
  return value !== null && TAB_KEYS.includes(value as TabKey)
}

function isSortKey(value: string | null): value is MeetingListSort {
  return value !== null && SORT_KEYS.includes(value as MeetingListSort)
}

/**
 * 会议列表页（Step 8B-6：placeholder 升级为真实列表）。
 * 状态 Tabs → GET /api/meetings?status=...（真实后端 status filter）。
 * 每条（MeetingListItem）：title / 状态 Tag / 创建或开始时间 / 真实时长 / 状态 CTA。
 * 无 fake 数据；空态 / loading / error 均轻量。
 */
export function MeetingsPage() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [renamingMeeting, setRenamingMeeting] = useState<Meeting | null>(null)
  const [renameTitle, setRenameTitle] = useState('')
  const [renameSaving, setRenameSaving] = useState(false)
  const requestedStatus = searchParams.get('status')
  const tab: TabKey = isTabKey(requestedStatus) ? requestedStatus : 'all'
  const requestedPage = Number(searchParams.get('page') ?? '1')
  const page = Number.isInteger(requestedPage) && requestedPage > 0 ? requestedPage : 1
  const keyword = searchParams.get('q')?.trim() ?? ''
  const createdFrom = searchParams.get('from') ?? ''
  const createdTo = searchParams.get('to') ?? ''
  const requestedSort = searchParams.get('sort')
  const sort: MeetingListSort = isSortKey(requestedSort) ? requestedSort : 'updated_desc'
  const [searchText, setSearchText] = useState(keyword)

  useEffect(() => setSearchText(keyword), [keyword])

  const commonFilters = useMemo(
    () => ({
      q: keyword || undefined,
      created_from: createdFrom || undefined,
      created_to: createdTo || undefined,
    }),
    [createdFrom, createdTo, keyword],
  )

  const query = useMemo(
    () => (tab === 'all'
      ? { ...commonFilters, sort, limit: PAGE_LIMIT, offset: (page - 1) * PAGE_LIMIT }
      : { ...commonFilters, status: tab, sort, limit: PAGE_LIMIT, offset: (page - 1) * PAGE_LIMIT }),
    [commonFilters, page, sort, tab],
  )
  const { meetings, total, loading, error, reload } = useMeetingList(query)
  const createdStats = useMeetingList({ ...commonFilters, status: 'CREATED', limit: 1 })
  const inProgressStats = useMeetingList({ ...commonFilters, status: 'IN_PROGRESS', limit: 1 })
  const finalizingStats = useMeetingList({ ...commonFilters, status: 'FINALIZING', limit: 1 })
  const completedStats = useMeetingList({ ...commonFilters, status: 'COMPLETED', limit: 1 })

  const statusStats = [createdStats, inProgressStats, finalizingStats, completedStats]
  const allStatsLoading = statusStats.some((item) => item.loading)
  const allStatsError = statusStats.some((item) => item.error)
  const allMeetingsTotal = statusStats.reduce((sum, item) => sum + item.total, 0)

  const setListLocation = ({
    nextTab = tab,
    nextPage = 1,
    nextKeyword = keyword,
    nextFrom = createdFrom,
    nextTo = createdTo,
    nextSort = sort,
  }: {
    nextTab?: TabKey
    nextPage?: number
    nextKeyword?: string
    nextFrom?: string
    nextTo?: string
    nextSort?: MeetingListSort
  } = {}) => {
    const params: Record<string, string> = {}
    if (nextTab !== 'all') params.status = nextTab
    if (nextPage > 1) params.page = String(nextPage)
    if (nextKeyword) params.q = nextKeyword
    if (nextFrom) params.from = nextFrom
    if (nextTo) params.to = nextTo
    if (nextSort !== 'updated_desc') params.sort = nextSort
    setSearchParams(params)
  }

  const resetFilters = () => {
    setSearchText('')
    setListLocation({ nextKeyword: '', nextFrom: '', nextTo: '', nextSort: 'updated_desc' })
  }

  const handleDelete = async (meeting: Meeting) => {
    setDeletingId(meeting.id)
    try {
      await deleteMeeting(meeting.id)
      message.success('会议已删除')
      createdStats.reload()
      inProgressStats.reload()
      finalizingStats.reload()
      completedStats.reload()

      if (meetings.length === 1 && page > 1) {
        setListLocation({ nextPage: page - 1 })
      } else {
        reload()
      }
    } catch (raw: unknown) {
      const apiError = normalizeApiError(raw)
      message.error(
        apiError.status === 409
          ? '会议状态已变化，进行中或收尾中的会议不能删除'
          : apiError.message,
      )
    } finally {
      setDeletingId(null)
    }
  }

  const openRename = (meeting: Meeting) => {
    setRenamingMeeting(meeting)
    setRenameTitle(meeting.title)
  }

  const handleRename = async () => {
    if (!renamingMeeting) return
    const title = renameTitle.trim()
    if (!title) {
      message.warning('请输入会议名称')
      return
    }
    if (title === renamingMeeting.title.trim()) {
      setRenamingMeeting(null)
      return
    }

    setRenameSaving(true)
    try {
      await updateMeeting(renamingMeeting.id, { title })
      message.success('会议名称已更新')
      setRenamingMeeting(null)
      reload()
    } catch (raw: unknown) {
      message.error(normalizeApiError(raw).message)
    } finally {
      setRenameSaving(false)
    }
  }

  useEffect(() => {
    if (loading) return
    const lastPage = Math.max(1, Math.ceil(total / PAGE_LIMIT))
    if (page > lastPage) setListLocation({ nextPage: lastPage })
  }, [loading, page, tab, total])

  const stats = [
    {
      key: 'all' as const,
      label: '全部会议',
      icon: <CalendarOutlined />,
      value: allMeetingsTotal,
      loading: allStatsLoading,
      error: allStatsError,
    },
    { key: 'CREATED' as const, label: '待开始', icon: <ClockCircleOutlined />, ...createdStats },
    { key: 'IN_PROGRESS' as const, label: '进行中', icon: <AudioOutlined />, ...inProgressStats },
    { key: 'FINALIZING' as const, label: '收尾中', icon: <Loading3QuartersOutlined />, ...finalizingStats },
    { key: 'COMPLETED' as const, label: '已完成', icon: <CheckCircleOutlined />, ...completedStats },
  ]

  return (
    <div className="page">
      <div className="section-header meetings-page__header">
        <div>
          <Title level={3} className="h-m0">
            会议
          </Title>
        </div>
        <Button icon={<HomeOutlined />} onClick={() => navigate('/')}>
          回到首页
        </Button>
      </div>

      <div className="panel meetings-panel">
        <div className="meetings-summary" aria-label="会议状态统计">
          {stats.map((item) => (
            <button
              key={item.key}
              type="button"
              className={`meetings-summary__item meetings-summary__item--${item.key.toLowerCase()}${tab === item.key ? ' is-active' : ''}`}
              aria-pressed={tab === item.key}
              onClick={() => setListLocation({ nextTab: item.key })}
            >
              <span className="meetings-summary__icon">{item.icon}</span>
              <span className="meetings-summary__copy">
                <span>{item.label}</span>
                <strong>{item.loading ? '--' : item.error ? '—' : item.value ?? item.total}</strong>
              </span>
            </button>
          ))}
        </div>

        <div className="meetings-toolbar" aria-label="会议查找与排序">
          <Input.Search
            className="meetings-toolbar__search"
            value={searchText}
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜索会议名称或会议号"
            enterButton="搜索"
            onChange={(event) => {
              const value = event.target.value
              setSearchText(value)
              if (!value && keyword) setListLocation({ nextKeyword: '' })
            }}
            onSearch={(value) => setListLocation({ nextKeyword: value.trim() })}
          />
          <DatePicker.RangePicker
            className="meetings-toolbar__dates"
            value={createdFrom && createdTo ? [dayjs(createdFrom), dayjs(createdTo)] : null}
            format="YYYY-MM-DD"
            placeholder={['开始日期', '结束日期']}
            allowClear
            onChange={(dates) => {
              setListLocation({
                nextFrom: dates?.[0]?.format('YYYY-MM-DD') ?? '',
                nextTo: dates?.[1]?.format('YYYY-MM-DD') ?? '',
              })
            }}
          />
          <Select
            className="meetings-toolbar__sort"
            value={sort}
            onChange={(value: MeetingListSort) => setListLocation({ nextSort: value })}
            options={[
              { value: 'updated_desc', label: '最近更新' },
              { value: 'created_desc', label: '最新创建' },
              { value: 'created_asc', label: '最早创建' },
            ]}
          />
          <Button
            className="meetings-toolbar__reset"
            disabled={!keyword && !createdFrom && !createdTo && sort === 'updated_desc'}
            onClick={resetFilters}
          >
            重置
          </Button>
        </div>

        <Tabs
          activeKey={tab}
          onChange={(key) => {
            const next = key as TabKey
            setListLocation({ nextTab: next })
          }}
          items={[
            { key: 'all', label: '全部' },
            { key: 'CREATED', label: '待开始' },
            { key: 'IN_PROGRESS', label: '进行中' },
            { key: 'FINALIZING', label: '收尾中' },
            { key: 'COMPLETED', label: '已完成' },
          ]}
        />

        <div className="meetings-list">
          {loading ? (
          <div className="meetings-list__state">
            <Skeleton active paragraph={{ rows: 3 }} title={false} />
          </div>
        ) : null}

        {!loading && error ? (
          <div className="meetings-list__state">
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
          <div className="meetings-list__state">
            <EmptyPlaceholder
              icon={<CalendarOutlined />}
              title="还没有会议记录"
              description={keyword || createdFrom || createdTo
                ? '没有找到符合当前条件的会议，可以调整或重置筛选。'
                : '创建会议后，实时转写与 AI 纪要将在这里沉淀。'}
              action={keyword || createdFrom || createdTo
                ? <Button size="small" onClick={resetFilters}>重置筛选</Button>
                : undefined}
              compact
            />
          </div>
        ) : null}

        {!loading && !error && meetings.length > 0 ? (
          <div>
            {meetings.map((m) => (
              <MeetingListItem
                key={m.id}
                meeting={m}
                onDelete={handleDelete}
                deleteLoading={deletingId === m.id}
                onRename={openRename}
              />
            ))}
          </div>
        ) : null}
        </div>

        {!loading && !error && total > 0 ? (
          <div className="meetings-pagination">
            <span>
              共 <strong>{total}</strong> 场会议
            </span>
            <Pagination
              current={page}
              pageSize={PAGE_LIMIT}
              total={total}
              showSizeChanger={false}
              showLessItems
              onChange={(nextPage) => setListLocation({ nextPage })}
            />
          </div>
        ) : null}
      </div>

      <Modal
        title="修改会议名称"
        open={renamingMeeting !== null}
        okText="保存"
        cancelText="取消"
        confirmLoading={renameSaving}
        okButtonProps={{ disabled: !renameTitle.trim() }}
        onOk={handleRename}
        onCancel={() => {
          if (!renameSaving) setRenamingMeeting(null)
        }}
        destroyOnClose
      >
        <Input
          value={renameTitle}
          maxLength={255}
          showCount
          autoFocus
          placeholder="请输入会议名称"
          onChange={(event) => setRenameTitle(event.target.value)}
          onPressEnter={handleRename}
        />
      </Modal>
    </div>
  )
}
