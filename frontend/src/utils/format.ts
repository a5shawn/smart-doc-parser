/**
 * 展示层格式化。
 *
 * 所有涉及时间的函数都显式按本地时区渲染——后端一律存 UTC 的 TIMESTAMPTZ，
 * 直接拿字符串展示会凭空差 8 小时。
 */

const USD_TO_CNY = 7.1

/** 把 ISO 时间字符串按浏览器本地时区渲染。 */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString('zh-CN', { hour12: false })
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleTimeString('zh-CN', { hour12: false })
}

/** 相对时间。列表里比绝对时间更好读。 */
export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'

  const diffMs = Date.now() - date.getTime()
  const seconds = Math.floor(diffMs / 1000)

  if (seconds < 60) return '刚刚'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`
  if (seconds < 2592000) return `${Math.floor(seconds / 86400)} 天前`
  return date.toLocaleDateString('zh-CN')
}

/**
 * 成本。单份文档的成本只有几厘钱，两位小数会全部显示成 ¥0.00，
 * 因此小额时增加小数位。
 */
export function formatCost(usd: number | null | undefined): string {
  const value = (usd ?? 0) * USD_TO_CNY
  if (value === 0) return '¥0'
  if (value < 0.01) return `¥${value.toFixed(4)}`
  if (value < 1) return `¥${value.toFixed(3)}`
  return `¥${value.toFixed(2)}`
}

export function formatBytes(bytes: number | null | undefined): string {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let unitIndex = 0
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024
    unitIndex += 1
  }
  return `${value.toFixed(unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1000) return `${ms} ms`
  return `${(ms / 1000).toFixed(2)} s`
}

export function formatNumber(value: number | null | undefined): string {
  return (value ?? 0).toLocaleString('zh-CN')
}

/**
 * 把任意值渲染成适合放进字段卡片的字符串。
 *
 * 注意 `false` 和 `0` 都是**有效值**，不能当成空值处理——
 * 用 `value == null` 而不是 `!value` 判断，否则「金额是 0」会显示成"未抽取到"。
 */
export function formatFieldValue(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value === 'number') return String(value)
  if (Array.isArray(value)) return value.length === 0 ? '' : JSON.stringify(value)
  return JSON.stringify(value)
}

/** 数组字段是否应当渲染成表格。 */
export function isObjectArray(value: unknown): value is Record<string, unknown>[] {
  return (
    Array.isArray(value) &&
    value.length > 0 &&
    value.every((item) => item !== null && typeof item === 'object' && !Array.isArray(item))
  )
}
