import { useState } from 'react'
import { Button, Input, Tooltip, message } from 'antd'
import { SendOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { normalizeApiError, sendDemoUtterance } from '@/api'
import { CAPTAIN_LABEL, DEMO_SPEAKERS } from '@/constants/captain'

/**
 * 演示输入条（会议室页底部）—— 让人**在真实会议里**打出几方对话，走完整链路。
 *
 * 为什么它是产品功能而不是调试开关
 * --------------------------------
 * 没有 ESP32 / Tencent ASR 在场时，这条输入是唯一能验证「真实 ASR 路径」的方式：
 * 它提交的每一句都会变成**持久化 FINAL**（走 `asr_service` 那条与硬件相同的
 * ingress），因此 SSE、Meeting State、水华队长唤醒全部按真实行为发生。
 *
 * 四条纪律
 * --------
 *  1. **序号不由前端指定**。请求体里没有 `segment_seq`；服务端回传什么就是什么。
 *     CLAUDE.md 的「segment_seq 由 Backend 生成」在前端同样成立。
 *  2. **唤醒判定不由前端推断**。这里只把文字发出去，是否请求了裁判由后端
 *     `wake_word.py` 判定并回传（`triggered_captain`）。
 *  3. **唤醒词不由前端持有**。快捷按钮发 `trigger_captain: true`，由服务端用
 *     自己配置的唤醒词组句 —— 前端硬编码会在部署改措辞后静默失效。
 *  4. **不碰全局 store**。FINAL 由 SSE 广播回来（本就是事实源的通路），
 *     组件不往 store 里塞自己伪造的 segment，否则会出现「列表里有一条，
 *     刷新后就没了」的假数据。
 */

interface DemoComposerProps {
  meetingId: string
  /** 会议未 IN_PROGRESS / FINALIZING 时禁用输入（后端也会 409，前端先拦住） */
  disabled: boolean
  /** 发送成功后回调（父层可以用来滚动到底部） */
  onSent?: (segmentSeq: number, triggeredCaptain: boolean) => void
}

export function DemoComposer({ meetingId, disabled, onSent }: DemoComposerProps) {
  const [speaker, setSpeaker] = useState<string>(DEMO_SPEAKERS[0].name)
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)

  /** 统一的发送路径：普通发言与快捷发送共用，保证两者行为完全一致。 */
  const send = async (payload: { text?: string; triggerCaptain?: boolean }) => {
    if (sending || disabled) return
    setSending(true)
    try {
      const ack = await sendDemoUtterance(meetingId, {
        speaker,
        ...(payload.triggerCaptain
          ? { trigger_captain: true }
          : { text: (payload.text ?? '').trim() }),
      })
      if (!payload.triggerCaptain) setText('')
      onSent?.(ack.segment_seq, ack.triggered_captain)
    } catch (raw: unknown) {
      message.error(normalizeApiError(raw).message)
    } finally {
      setSending(false)
    }
  }

  const submit = (value: string) => {
    const content = value.trim()
    if (!content) return
    void send({ text: content })
  }

  // 快捷发送：服务端用它自己配置的唤醒词组一句发言并落库。
  // 刻意**不复用** POST /captain —— 走同一句话、同一条 ingress 才是真的在验证
  // 唤醒链路；直接调 trigger API 会绕过 wake_word，测不到它。
  const quickSendCaptain = () => void send({ triggerCaptain: true })

  return (
    <div className={`demo-composer${disabled ? ' demo-composer--disabled' : ''}`}>
      <div className="demo-composer__head">
        <span className="demo-composer__eyebrow">演示输入</span>
        <span className="demo-composer__hint">
          发送后生成真实转写，AI 与裁判按正常流程响应
        </span>
      </div>

      <div className="demo-composer__speakers" role="radiogroup" aria-label="选择发言人">
        {DEMO_SPEAKERS.map((s) => (
          <button
            key={s.name}
            type="button"
            role="radio"
            aria-checked={speaker === s.name}
            className={`demo-speaker${speaker === s.name ? ' demo-speaker--on' : ''}`}
            style={{ ['--speaker-tint' as string]: s.color }}
            disabled={disabled}
            onClick={() => setSpeaker(s.name)}
          >
            <span className="demo-speaker__dot" aria-hidden />
            {s.name}
          </button>
        ))}
      </div>

      <div className="demo-composer__row">
        <Tooltip title="让水华队长现在分析最近的讨论">
          <Button
            className="demo-composer__quick"
            icon={<ThunderboltOutlined />}
            disabled={disabled}
            onClick={quickSendCaptain}
          >
            {CAPTAIN_LABEL}
          </Button>
        </Tooltip>

        <Input.TextArea
          className="demo-composer__input"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={`以「${speaker}」的身份说一句…`}
          autoSize={{ minRows: 1, maxRows: 4 }}
          disabled={disabled}
          maxLength={4000}
          onPressEnter={(e) => {
            // Enter 发送，Shift+Enter 换行 —— 与常见 IM 一致
            if (!e.shiftKey) {
              e.preventDefault()
              submit(text)
            }
          }}
        />

        <Button
          type="primary"
          className="demo-composer__send"
          icon={<SendOutlined />}
          loading={sending}
          disabled={disabled || !text.trim()}
          onClick={() => submit(text)}
        >
          发送
        </Button>
      </div>
    </div>
  )
}
