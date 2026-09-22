import { useState } from 'react'
import { Button } from 'antd'
import {
  AudioOutlined,
  CalendarOutlined,
  CheckCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  RightOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { CreateMeetingModal } from '@/components/CreateMeetingModal'
import { HomeProductShowcase } from '@/components/HomeProductShowcase'
import { useMeetingList } from '@/hooks/useMeetingList'

/** 首页只读取状态概况，不承载会议浏览；完整列表统一进入会议页。 */
const HOME_LIST_LIMIT = 100

/**
 * 首页是会议工作台入口：价值表达、能力链路、会议状态导航。
 * 会议浏览与管理统一交给 /meetings，避免首页和会议页重复。
 * 创建 ≠ 开始：点击只创建（status=CREATED），然后进入会前准备页。
 * 所有统计均来自后端真实列表，不新增 fake 数据。
 */
export function HomePage() {
  const [modalOpen, setModalOpen] = useState(false)
  const navigate = useNavigate()
  const { meetings, total, loading, error, reload } = useMeetingList({
    limit: HOME_LIST_LIMIT,
    offset: 0,
  })

  const activeCount = meetings.filter((m) => m.status === 'IN_PROGRESS').length
  const completedCount = meetings.filter((m) => m.status === 'COMPLETED').length

  return (
    <div className="page page--home">
      <section className="home-workspace">
        <div className="home-workspace__head">
          <div className="home-hero-signal" aria-hidden>
            <svg
              className="home-hero-flow"
              viewBox="0 0 1000 220"
              preserveAspectRatio="none"
            >
              <path
                className="home-hero-flow__line home-hero-flow__line--rose"
                d="M-30 146 C 150 98, 278 184, 454 128 S 760 62, 1030 106"
              />
              <path
                className="home-hero-flow__line home-hero-flow__line--mint"
                d="M-40 178 C 174 222, 314 92, 512 152 S 806 214, 1040 132"
              />
            </svg>
            <div className="home-hero-wave">
              {Array.from({ length: 9 }, (_, index) => <span key={index} />)}
            </div>
          </div>
          <div className="home-workspace__copy">
            <h1 className="home-workspace__title">让每一次会议，都有清晰结果</h1>
            <p className="home-workspace__subtitle">
              实时记录讨论，持续理解会议，会后自动沉淀纪要与待办
            </p>
          </div>
          <div className="home-workspace__actions">
            <Button
              type="primary"
              size="large"
              icon={<PlusOutlined />}
              onClick={() => setModalOpen(true)}
            >
              创建会议
            </Button>
            <Button size="large" icon={<CalendarOutlined />} onClick={() => navigate('/meetings')}>
              查看会议
            </Button>
          </div>
        </div>

        <HomeProductShowcase />

        <nav className="home-overview" aria-label="会议状态导航">
          <button
            type="button"
            className="home-overview__item"
            onClick={() => navigate('/meetings?status=all')}
          >
            <span className="home-overview__icon"><CalendarOutlined /></span>
            <span className="home-overview__copy">
              <span>全部会议</span>
              <strong>{loading ? '--' : total}</strong>
            </span>
            <RightOutlined className="home-overview__arrow" aria-hidden />
          </button>
          <button
            type="button"
            className="home-overview__item"
            onClick={() => navigate('/meetings?status=IN_PROGRESS')}
          >
            <span className="home-overview__icon home-overview__icon--live"><AudioOutlined /></span>
            <span className="home-overview__copy">
              <span>进行中</span>
              <strong>{loading ? '--' : activeCount}</strong>
            </span>
            <RightOutlined className="home-overview__arrow" aria-hidden />
          </button>
          <button
            type="button"
            className="home-overview__item"
            onClick={() => navigate('/meetings?status=COMPLETED')}
          >
            <span className="home-overview__icon home-overview__icon--done"><CheckCircleOutlined /></span>
            <span className="home-overview__copy">
              <span>已完成</span>
              <strong>{loading ? '--' : completedCount}</strong>
            </span>
            <RightOutlined className="home-overview__arrow" aria-hidden />
          </button>
        </nav>

        {error ? (
          <div className="home-status-error" role="status">
            <span>会议状态暂时无法同步</span>
            <Button type="link" size="small" icon={<ReloadOutlined />} onClick={reload}>
              重试
            </Button>
          </div>
        ) : null}
      </section>

      <CreateMeetingModal open={modalOpen} onClose={() => setModalOpen(false)} />
    </div>
  )
}
