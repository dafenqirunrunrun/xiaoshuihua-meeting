import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Form, Input, Modal, message } from 'antd'
import { createMeeting, startMeeting } from '@/api'
import { normalizeApiError } from '@/api'

interface CreateMeetingModalProps {
  open: boolean
  onClose: () => void
}

/** 创建会议 Modal（fronted_designer §11）。点击「创建并开始」→ POST /meetings → POST /start → 进入 Meeting Room。 */
export function CreateMeetingModal({ open, onClose }: CreateMeetingModalProps) {
  const [form] = Form.useForm()
  const [submitting, setSubmitting] = useState(false)
  const navigate = useNavigate()

  const handleCreate = async () => {
    try {
      const values = await form.validateFields()
      setSubmitting(true)
      const created = await createMeeting(values)
      const started = await startMeeting(created.id)
      message.success('会议已创建')
      onClose()
      navigate(`/meetings/${started.id}`)
    } catch (error: unknown) {
      // 表单校验失败：error 是 { errorFields }，直接放行，不弹错误
      if (typeof error === 'object' && error !== null && 'errorFields' in error) {
        return
      }
      if (error instanceof Error && 'kind' in error) {
        message.error(normalizeApiError(error).message)
      } else if (error && typeof error === 'object' && 'response' in (error as object)) {
        message.error(normalizeApiError(error).message)
      } else {
        message.error('创建失败，请稍后重试')
      }
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
      okText="创建并开始"
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
