/**
 * 文件下载。
 *
 * **走 Blob 而不是让后端设置 Content-Disposition**：
 * 中文文件名放进响应头需要按 RFC 5987 编码成 `filename*=UTF-8''%E5%90%88...`，
 * 而不同浏览器与中间代理对它的支持参差不齐，很容易变成乱码或被截断。
 * 用 `URL.createObjectURL` + `<a download>` 把文件名交给浏览器本地处理，
 * 整类编码问题就消失了。
 */

export function downloadBlob(content: BlobPart, filename: string, mimeType = 'application/json') {
  const blob = content instanceof Blob ? content : new Blob([content], { type: mimeType })
  const url = URL.createObjectURL(blob)

  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.style.display = 'none'
  document.body.appendChild(anchor)
  anchor.click()

  // 必须清理：objectURL 会让浏览器一直持有这块内存直到页面卸载
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export function downloadJson(data: unknown, filename: string) {
  downloadBlob(JSON.stringify(data, null, 2), filename)
}

/** 复制到剪贴板。降级到 execCommand 以兼容非 HTTPS 环境（Clipboard API 需要安全上下文）。 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
      return true
    }
    return legacyCopy(text)
  } catch {
    return legacyCopy(text)
  }
}

function legacyCopy(text: string): boolean {
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.style.position = 'fixed'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  textarea.select()
  try {
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    document.body.removeChild(textarea)
  }
}
