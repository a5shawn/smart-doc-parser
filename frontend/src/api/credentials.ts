/**
 * 接口密钥的读写。
 *
 * 单独成模块是为了解开循环依赖：设置页的 store 需要写它，
 * axios 拦截器需要读它，两者都依赖这个模块而不是互相依赖。
 *
 * **已知局限**：密钥存在 localStorage 里，任何 XSS 都能读走它。
 * 这是需求指定的方案（演示用，避免引入完整登录体系），
 * 生产环境的正确做法是后端会话 + HttpOnly Cookie，
 * 或者干脆把密钥留在服务端代理层，不下发到浏览器。
 * 这一点在 README 与设置页里都明确写出，而不是假装不存在。
 */

const STORAGE_KEY = 'smart-doc-parser:api-key'

export function getApiKey(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) ?? ''
  } catch {
    // 隐私模式下 localStorage 可能不可用，此时退化为不带密钥
    return ''
  }
}

export function setApiKey(value: string): void {
  try {
    if (value) {
      localStorage.setItem(STORAGE_KEY, value)
    } else {
      localStorage.removeItem(STORAGE_KEY)
    }
  } catch {
    // 存不进去也不该让页面崩掉
  }
}
