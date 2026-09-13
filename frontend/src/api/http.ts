/**
 * axios 实例与统一错误处理。
 *
 * 后端所有错误都是同一个信封结构（见 docs/api.md），
 * 这里把它转成带类型的 `ApiError`，让调用方拿到 `code` 而不是去解析字符串。
 */

import axios, { AxiosError, type AxiosInstance, type AxiosRequestConfig } from 'axios'

import { getApiKey } from './credentials'

/** 后端返回的错误信封。 */
interface ErrorEnvelope {
  error?: {
    code?: string
    message?: string
    details?: Record<string, unknown>
    request_id?: string
  }
}

export class ApiError extends Error {
  readonly code: string
  readonly status: number | undefined
  readonly details: Record<string, unknown>
  readonly requestId: string | undefined

  constructor(options: {
    code: string
    message: string
    status?: number
    details?: Record<string, unknown>
    requestId?: string
  }) {
    super(options.message)
    this.name = 'ApiError'
    this.code = options.code
    this.status = options.status
    this.details = options.details ?? {}
    this.requestId = options.requestId
  }

  /** 是否需要用户去设置页配置接口密钥。 */
  get isAuthError(): boolean {
    return this.code === 'UNAUTHORIZED'
  }

  /** 是否是可以靠重试解决的问题。 */
  get isRetryable(): boolean {
    return this.code.startsWith('LLM_') || this.code === 'SERVICE_UNAVAILABLE'
  }
}

/**
 * 接口基地址。默认是相对路径 `/api/v1`——这一点很关键：
 * Docker 部署时由 nginx 反代、本地开发时由 Vite 代理，
 * **两种形态下前端代码完全一致，不需要任何环境判断**。
 */
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api/v1'

export const http: AxiosInstance = axios.create({
  baseURL: API_BASE_URL,
  timeout: 60_000,
})

http.interceptors.request.use((config) => {
  const apiKey = getApiKey()
  if (apiKey) {
    config.headers.set('X-API-Key', apiKey)
  }
  return config
})

http.interceptors.response.use(
  (response) => response,
  (error: AxiosError<ErrorEnvelope>) => {
    // 网络层面失败：连不上、超时、被 CORS 拦截
    if (!error.response) {
      const isTimeout = error.code === 'ECONNABORTED'
      return Promise.reject(
        new ApiError({
          code: isTimeout ? 'CLIENT_TIMEOUT' : 'NETWORK_ERROR',
          message: isTimeout
            ? '请求超时，服务可能正在处理大文件，请稍后重试'
            : '无法连接到服务，请确认后端已启动',
        }),
      )
    }

    const envelope = error.response.data?.error
    return Promise.reject(
      new ApiError({
        code: envelope?.code ?? `HTTP_${error.response.status}`,
        message: envelope?.message ?? error.message ?? '请求失败',
        status: error.response.status,
        details: envelope?.details,
        requestId: envelope?.request_id,
      }),
    )
  },
)

/** 带上类型的 GET/POST 包装，省去每处都写 `res.data`。 */
export async function get<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
  const response = await http.get<T>(url, config)
  return response.data
}

export async function post<T>(
  url: string,
  data?: unknown,
  config?: AxiosRequestConfig,
): Promise<T> {
  const response = await http.post<T>(url, data, config)
  return response.data
}

export async function del<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
  const response = await http.delete<T>(url, config)
  return response.data
}
