import { http } from './http'
import type {
  CaptainReportPublic,
  CaptainTriggerAccepted,
} from '@/types/captain'

/**
 * 水华队长（Captain）读取入口（ADR-002 §6）。
 *
 * `GET .../captain/latest` 与 `.../captain/{report_id}` 是**事实源**。
 * `captain.completed` SSE 只是快路径（payload 顺带携带报告内容以便即时渲染），
 * 断线 / 刷新 / 重连一律回到这两个 GET 恢复 —— SSE 不可回放。
 *
 * 会议从未被裁判过 → 404。这是**正常状态**（不是错误）：调用方据此显示
 * 「还没有裁判报告」，而不是报错。
 */
export async function getLatestCaptainReport(
  meetingId: string,
): Promise<CaptainReportPublic> {
  const { data } = await http.get<CaptainReportPublic>(
    `/api/meetings/${meetingId}/captain/latest`,
  )
  return data
}

/** 按 id 读取一份报告（SSE 断线后的恢复读）。 */
export async function getCaptainReport(
  meetingId: string,
  reportId: string,
): Promise<CaptainReportPublic> {
  const { data } = await http.get<CaptainReportPublic>(
    `/api/meetings/${meetingId}/captain/${reportId}`,
  )
  return data
}

/**
 * 请求一次裁判。**不传 anchor**：按钮路径没有「哪一句触发的」这一信息，
 * 由服务端取当前高水位；唤醒词路径由服务端自己锚定（前端无法、也不应伪造）。
 *
 * 202 → 分析在独立 dispatch worker 上跑；结果走 SSE + GET 恢复。
 */
export async function triggerCaptain(
  meetingId: string,
): Promise<CaptainTriggerAccepted> {
  const { data } = await http.post<CaptainTriggerAccepted>(
    `/api/meetings/${meetingId}/captain`,
  )
  return data
}
