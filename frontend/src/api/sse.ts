/**
 * SSE 客户端。
 *
 * 为什么不用浏览器原生的 `EventSource`
 * ------------------------------------
 * **它无法设置请求头**，因此带不了 `X-API-Key`。绕过办法只有把密钥放进
 * query string（会出现在服务器访问日志、浏览器历史、Referer 里），代价太大。
 *
 * 为什么不用 axios
 * ----------------
 * axios 不暴露流式读取能力，它会等整个响应结束才回调——
 * 对 SSE 来说等于完全失去意义。
 *
 * 因此用原生 `fetch` + `ReadableStream` 手动解析。多写的这几十行换来了
 * 完整的控制权：自定义请求头、主动中止、可控重连。
 */

import { SseParser, type SseEvent } from '@/utils/sseParse'

import { getApiKey } from './credentials'
import { API_BASE_URL } from './http'

export type SseState = 'connecting' | 'open' | 'reconnecting' | 'closed'

export interface SseConnectionOptions {
  /** 相对 API_BASE_URL 的路径，例如 `/tasks/stream` */
  path: string
  params?: Record<string, string | undefined>
  onEvent: (event: SseEvent) => void
  onStateChange?: (state: SseState) => void
}

export interface SseConnection {
  close: () => void
}

/** 重连退避的上限。再长的话用户会觉得界面"死"了。 */
const MAX_BACKOFF_MS = 15_000
const INITIAL_BACKOFF_MS = 1_000

export function createSseConnection(options: SseConnectionOptions): SseConnection {
  const { path, params, onEvent, onStateChange } = options

  const parser = new SseParser()
  const abortController = new AbortController()
  let closedByCaller = false
  let attempt = 0
  let retryTimer: ReturnType<typeof setTimeout> | undefined

  const buildUrl = (): string => {
    const query = new URLSearchParams()
    for (const [key, value] of Object.entries(params ?? {})) {
      if (value !== undefined && value !== '') query.set(key, value)
    }
    const suffix = query.toString()
    return `${API_BASE_URL}${path}${suffix ? `?${suffix}` : ''}`
  }

  const scheduleReconnect = () => {
    if (closedByCaller) return

    // 指数退避：服务刚重启时立刻重连只会连续失败，把日志刷满
    const delay = Math.min(INITIAL_BACKOFF_MS * 2 ** attempt, MAX_BACKOFF_MS)
    attempt += 1
    onStateChange?.('reconnecting')

    retryTimer = setTimeout(() => {
      void connect()
    }, delay)
  }

  const connect = async (): Promise<void> => {
    if (closedByCaller) return
    onStateChange?.(attempt === 0 ? 'connecting' : 'reconnecting')

    const headers: Record<string, string> = { Accept: 'text/event-stream' }
    const apiKey = getApiKey()
    if (apiKey) headers['X-API-Key'] = apiKey

    try {
      const response = await fetch(buildUrl(), {
        headers,
        signal: abortController.signal,
        // 明确禁止浏览器缓存这个响应
        cache: 'no-store',
      })

      if (!response.ok) {
        if (response.status === 401) {
          // 密钥不对时重连没有意义，直接把状态置为关闭让界面提示用户去配置
          onStateChange?.('closed')
          return
        }
        throw new Error(`SSE 连接失败：HTTP ${response.status}`)
      }
      if (!response.body) {
        throw new Error('响应没有可读流')
      }

      attempt = 0
      parser.reset()
      onStateChange?.('open')

      const reader = response.body.getReader()
      // **必须带 { stream: true }**：中文是 UTF-8 三字节，
      // 一个汉字很可能被切在两个网络分片之间。没有这个选项的话，
      // 被切断的字节会解码成 U+FFFD 替换字符，界面上的中文变成乱码。
      const decoder = new TextDecoder('utf-8')

      for (;;) {
        const { done, value } = await reader.read()
        if (done) break

        const text = decoder.decode(value, { stream: true })
        for (const event of parser.push(text)) {
          onEvent(event)
        }
      }

      // 流结束了，把缓冲区里可能残留的最后一条消息发出来。
      // 不做这一步的话，服务端在报文末尾断开时最后一条事件会丢失——
      // 表现为进度条停在 95% 不动，而实际任务已经完成。
      for (const event of parser.flush()) {
        onEvent(event)
      }

      if (!closedByCaller) scheduleReconnect()
    } catch (error) {
      if (closedByCaller || abortController.signal.aborted) return
      // 网络抖动、服务重启都会走到这里，安静地退避重连即可，
      // 把噪声留给 onStateChange 去驱动界面上的"重连中"提示
      console.warn('[sse] 连接中断，将自动重连', error)
      scheduleReconnect()
    }
  }

  void connect()

  return {
    close: () => {
      closedByCaller = true
      if (retryTimer !== undefined) clearTimeout(retryTimer)
      abortController.abort()
      onStateChange?.('closed')
    },
  }
}
