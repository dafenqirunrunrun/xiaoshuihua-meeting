/**
 * Meeting 状态：严格对应对应后端四态（PRD §11 / ADR D1）。
 * 禁止出现 FINALIZING_FAILED / FAILED 等非四态值 —— Meeting 没有失败态，
 * 纪要失败由 SummaryStatus.FAILED 表达（meeting=COMPLETED, summary=FAILED）。
 */
export type MeetingStatus =
  | 'CREATED'
  | 'IN_PROGRESS'
  | 'FINALIZING'
  | 'COMPLETED'

/** POST /api/meetings 请求体（PRD §37） */
export interface MeetingCreate {
  title: string
  description?: string | null
}

export interface MeetingUpdate {
  title: string
}

export type MeetingListSort = 'updated_desc' | 'created_desc' | 'created_asc'

/** GET /api/meetings/:id 返回（MeetingOut） */
export interface Meeting {
  id: string
  meeting_code: string
  title: string
  description: string | null
  status: MeetingStatus
  started_at: string | null
  ended_at: string | null
  completed_at: string | null
  created_at: string | null
  updated_at: string | null
  /** 由后端推导的业务展示态（= status；FE 依赖驱动页面，见 MeetingStatusTag） */
  displayStatus?: MeetingStatus
}

/** GET /api/meetings 查询参数（Step 8B-6：status 过滤 + limit/offset 分页） */
export interface MeetingListQuery {
  status?: MeetingStatus
  q?: string
  created_from?: string
  created_to?: string
  sort?: MeetingListSort
  limit?: number
  offset?: number
}

/** GET /api/meetings 响应（MeetingListOut —— 复用 MeetingOut，不新增派生字段） */
export interface MeetingList {
  items: Meeting[]
  total: number
  limit: number
  offset: number
}
