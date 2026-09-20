import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Form, Input, Modal, message } from 'antd'
import { createMeeting } from '@/api'
import { normalizeApiError } from '@/api'

interface CreateMeetingModalProps {
  open: boolean
  onClose: () => void
}

/**
 * 创建会议 Modal（fronted_designer §11，8B-1 修订）。
 * 点击「创建会议」→ POST /api/meetings → meeting.status = CREATED → navigate(Meeting Room)。
 * 不自动 /start —— 创建 ≠ 开始；开始由用户在会前准备页主动触发（8B-2）。
 */
export function CreateMeetingModal({ open, onClose }: CreateMeetingModalProps) {
  const [form] = Form.useForm()
  const [submitting, setSubmitting] = useState(false)
  const navigate = useNavigate()

  const handleCreate = async () => {
    try {
      const values = await form.validateFields()
      setSubmitting(true)
      const created = await createMeeting(values)
      message.success('会议已创建')
      onClose()
      navigate(`/meetings/${created.id}`)
    } catch (error: unknown) {
      // 表单校验失败：error 是 { errorFields }，直接放行，不弹错误
      if (typeof error === 'object' && error !== null && 'errorFields' in error) {
        return
      }
      message.error(normalizeApiError(error).message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal
      title="创建新会议"
      open={open}
      onOk={handleCreate}
      onCancel={() => {
        form.resetFields()
        onClose()
      }}
      okText="创建会议"
      cancelText="取消"
      confirmLoading={submitting}
      destroyOnClose
    >
      <Form form={form} layout="vertical" requiredMark={false}>
        <Form.Item
          name="title"
          label="会议标题"
          rules={[{ required: true, whitespace: true, message: '请输入会议标题' }]}
        >
          <Input placeholder="例如：小水华 MVP 方案讨论" maxLength={255} />
        </Form.Item>
        <Form.Item name="description" label="会议描述（可选）">
          <Input.TextArea placeholder="补充会议背景" rows={3} maxLength={2000} />
        </Form.Item>
      </Form>
    </Modal>
  )
}
