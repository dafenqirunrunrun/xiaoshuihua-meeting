import axios from 'axios'

/**
 * 统一 Axios instance（API 层唯一出口，页面组件不得直接引用 axios）。
 * baseURL 不硬编码后端 host：开发走 Vite proxy（/api -> http://127.0.0.1:8000），
 * 部署可用 VITE_API_BASE_URL 覆盖（见 vite.config.ts / .env.example）。
 */
export const http = axios.create({
  timeout: 15000,
})

/**
 * 极薄 API 错误归一化。
 * 只区分三类：HTTP 状态错误（业务 detail 透传 / 后端 409/400 detail 转成 detail）、
 * 网络错误、未知。不搞复杂 Error Framework。
 */
export type ApiErrorKind = 'http' | 'network' | 'unknown'

export class ApiError extends Error {
  readonly kind: ApiErrorKind
  readonly status?: number
  readonly detail?: string

  constructor(kind: ApiErrorKind, message: string, status?: number, detail?: string) {
    super(message)
    this.name = 'ApiError'
    this.kind = kind
    this.status = status
    this.detail = detail
  }
}

/** 统一响应体：后端错误为 { detail: string } */
interface ErrorBody {
  detail?: unknown
}

/** 判断浏览器网络层根本没通（无 axios 响应） */
function isNetworkError(error: unknown): error is { request?: unknown } {
  return (
    typeof error === 'object' &&
    error !== null &&
    'request' in error &&
    (error as { response?: unknown }).response === undefined
  )
}

export function normalizeApiError(error: unknown): ApiError {
  // HTTP 状态错误：axios 有 response
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const response = (error as { response: { status?: number; data?: unknown } }).response
    const status = response?.status
    let detail: string | undefined

    const body = response?.data as ErrorBody | undefined
    if (body && typeof body.detail === 'string') {
      detail = body.detail
    }

    const msg =
      detail ?? (status ? `请求失败（HTTP ${status}）` : '请求失败')
    return new ApiError('http', msg, status, detail)
  }

  // 网络错误：无响应（连接失败 / 跨域 / 服务器不可达）
  if (isNetworkError(error)) {
    return new ApiError('network', '网络连接失败，请确认后端服务已启动')
  }

  // 其它未知
  if (error instanceof Error && error.message) {
    return new ApiError('unknown', error.message)
  }
  return new ApiError('unknown', '发生未知错误')
}
