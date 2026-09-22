import { useEffect, useState } from 'react'
import {
  AudioOutlined,
  BulbOutlined,
  CheckCircleOutlined,
  FileTextOutlined,
  MessageOutlined,
  QuestionCircleOutlined,
} from '@ant-design/icons'

const STORY_INTERVAL_MS = 5600

const stories = [
  { key: 'conversation', label: '实时记录', icon: <AudioOutlined /> },
  { key: 'understanding', label: '会议理解', icon: <BulbOutlined /> },
  { key: 'summary', label: '行动纪要', icon: <FileTextOutlined /> },
] as const

/** 首页产品展台：自动轮播核心能力，并允许用户通过分段导航直接切换。 */
export function HomeProductShowcase() {
  const [activeIndex, setActiveIndex] = useState(0)
  const [paused, setPaused] = useState(false)

  useEffect(() => {
    if (paused || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    const timer = window.setInterval(() => {
      setActiveIndex((index) => (index + 1) % stories.length)
    }, STORY_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [paused])

  return (
    <div
      className={`product-showcase${paused ? ' product-showcase--paused' : ''}`}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocusCapture={() => setPaused(true)}
      onBlurCapture={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setPaused(false)
      }}
    >
      <div
        className="product-showcase__track"
        style={{ transform: `translateX(-${activeIndex * 100}%)` }}
      >
        <article className="product-story product-story--conversation">
          <StoryCopy
            eyebrow="LIVE CONVERSATION"
            title="讨论发生时，内容已经清晰可见"
            description="发言自动归属，说话内容持续更新。每一次表达，都在同一条会议脉络里自然沉淀。"
          />
          <div className="conversation-scene" aria-hidden>
            <div className="conversation-scene__status"><span /> 正在记录</div>
            <div className="conversation-line conversation-line--one">
              <span className="conversation-avatar">01</span>
              <div><strong>发言人 01</strong><p>我们先确认这一轮的目标和时间节点。</p></div>
            </div>
            <div className="conversation-line conversation-line--two">
              <span className="conversation-avatar conversation-avatar--mint">02</span>
              <div><strong>发言人 02</strong><p>方案今天确认，明天下午完成第一版。</p></div>
            </div>
            <div className="conversation-wave">
              {Array.from({ length: 18 }, (_, index) => <span key={index} />)}
            </div>
          </div>
        </article>

        <article className="product-story product-story--understanding">
          <StoryCopy
            eyebrow="MEETING INTELLIGENCE"
            title="不是等散会，AI 在过程中持续理解"
            description="核心议题、明确决策和未决问题随讨论推进而更新，让团队始终知道会议进行到了哪里。"
          />
          <div className="understanding-scene" aria-hidden>
            <div className="understanding-scene__head">
              <span><BulbOutlined /> AI 会议理解</span>
              <small><i /> 刚刚更新</small>
            </div>
            <InsightRow icon={<MessageOutlined />} label="核心议题" text="确定首版功能边界与交付节奏" />
            <InsightRow icon={<CheckCircleOutlined />} label="已确认决策" text="本周完成真实会议链路验证" tone="mint" />
            <InsightRow icon={<QuestionCircleOutlined />} label="未决问题" text="确认多人场景中的发言人命名方式" tone="amber" />
          </div>
        </article>

        <article className="product-story product-story--summary">
          <StoryCopy
            eyebrow="ACTIONABLE NOTES"
            title="会议结束，结论已经成为下一步"
            description="纪要不再是一篇流水账。决定、责任人与待办被重新组织，讨论自然转化为可以执行的结果。"
          />
          <div className="summary-scene" aria-hidden>
            <div className="summary-paper">
              <div className="summary-paper__head">
                <span><FileTextOutlined /></span>
                <div><strong>产品评审会议纪要</strong><small>刚刚生成</small></div>
              </div>
              <div className="summary-paper__rule" />
              <div className="summary-decision"><CheckCircleOutlined /> 首版范围已确认</div>
              <div className="summary-task"><span>01</span><p>完成联调与验收清单</p><b>明天</b></div>
              <div className="summary-task"><span>02</span><p>整理多人会议测试样本</p><b>周五</b></div>
            </div>
          </div>
        </article>
      </div>

      <div className="product-showcase__nav" role="tablist" aria-label="产品能力展示">
        {stories.map((story, index) => (
          <button
            key={story.key}
            type="button"
            role="tab"
            aria-selected={activeIndex === index}
            className={activeIndex === index ? 'is-active' : ''}
            onClick={() => setActiveIndex(index)}
          >
            <span className="product-showcase__nav-icon">{story.icon}</span>
            <span>{story.label}</span>
            <i className="product-showcase__progress" aria-hidden />
          </button>
        ))}
      </div>
    </div>
  )
}

function StoryCopy({
  eyebrow,
  title,
  description,
}: {
  eyebrow: string
  title: string
  description: string
}) {
  return (
    <div className="product-story__copy">
      <span className="product-story__eyebrow">{eyebrow}</span>
      <h2>{title}</h2>
      <p>{description}</p>
    </div>
  )
}

function InsightRow({
  icon,
  label,
  text,
  tone = 'rose',
}: {
  icon: React.ReactNode
  label: string
  text: string
  tone?: 'rose' | 'mint' | 'amber'
}) {
  return (
    <div className={`insight-row insight-row--${tone}`}>
      <span className="insight-row__icon">{icon}</span>
      <div><small>{label}</small><strong>{text}</strong></div>
    </div>
  )
}
