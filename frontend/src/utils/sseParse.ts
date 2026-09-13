/**
 * SSE 协议解析器（纯函数，无副作用）。
 *
 * 单独抽出来的理由：**分片边界**是这里唯一真正难的地方。
 * 一个网络分片可能把一行切成两半，甚至把 `\r\n` 切成 `\r` + `\n`；
 * 如果按"收到一个 chunk 就当一条完整消息"来处理，遇到中文（UTF-8 三字节）
 * 或长 JSON 必然解析出错，而且表现为"偶尔丢事件"这种极难复现的 bug。
 *
 * 因此这里实现成一个有状态的解析器：喂进任意长度的分片，吐出完整的、解析好的事件。
 */

/** 服务端下发的一条消息。 */
export interface SseEvent {
  /** 事件名。后端未指定时为 'message'（SSE 协议默认值）。 */
  event: string
  /** 数据载荷。多条 data 行会以换行拼接。 */
  data: string
  /** 事件 ID。后端未下发时为 undefined。 */
  id?: string
}

/**
 * 行结束符。SSE 协议允许 `\r\n`、`\n`、`\r` 三种。
 *
 * 注意这里**不能**简单地 `replace(/\r\n/g, '\n')`：
 * 如果一个分片正好在 `\r` 之后结束，下一次分片以 `\n` 开头，
 * 提前替换会把两个分片各自看成一次换行，凭空多出一行。
 */
const LINE_END = /\r\n|\n|\r/

export class SseParser {
  private buffer = ''
  private eventName = ''
  private dataLines: string[] = []
  private lastEventId: string | undefined

  /** 喂入一个分片，返回其中**已经完整**的事件。不完整的部分留在缓冲区等下一个分片。 */
  push(chunk: string): SseEvent[] {
    this.buffer += chunk
    const events: SseEvent[] = []

    for (;;) {
      const match = LINE_END.exec(this.buffer)
      if (!match) break

      // 末尾单独的 \r 可能是 \r\n 的前半截，留到下一个分片再判断
      if (match[0] === '\r' && match.index === this.buffer.length - 1) break

      const line = this.buffer.slice(0, match.index)
      this.buffer = this.buffer.slice(match.index + match[0].length)

      const event = this.consumeLine(line)
      if (event) events.push(event)
    }

    return events
  }

  /**
   * 流已结束，把缓冲区里剩下的内容按"不会再来了"处理。
   *
   * 必须有这个方法：``push`` 会把末尾单独的 ``\\r`` 当成 ``\\r\\n`` 的前半截
   * 留到下一个分片——这对进行中的流是正确的，但流结束时那个 ``\\n``
   * 永远不会到，最后一条消息就永远派发不出来。
   */
  flush(): SseEvent[] {
    const pending = this.buffer
    this.buffer = ''

    // 需要**两个**换行，少一个都不行：
    // 第一个结束可能残留的半行（`data: 1`），第二个才是触发派发的空行。
    // 只补一个的话，那半行被正确地解析了，却因为缺少结束空行而永远发不出去。
    const tail = `${pending ? pending.replace(/\r$/, '\n') : ''}\n\n`

    return this.push(tail)
  }

  /** 清空状态。连接重建时调用。 */
  reset(): void {
    this.buffer = ''
    this.eventName = ''
    this.dataLines = []
    this.lastEventId = undefined
  }

  private consumeLine(line: string): SseEvent | null {
    // 空行表示一条消息结束，可以派发了
    if (line === '') {
      if (this.dataLines.length === 0 && this.eventName === '') return null

      const event: SseEvent = {
        event: this.eventName || 'message',
        data: this.dataLines.join('\n'),
      }
      if (this.lastEventId !== undefined) event.id = this.lastEventId

      this.eventName = ''
      this.dataLines = []
      return event
    }

    // 以冒号开头的是注释。后端用它做心跳，客户端应当忽略。
    if (line.startsWith(':')) return null

    const colonIndex = line.indexOf(':')
    const field = colonIndex === -1 ? line : line.slice(0, colonIndex)
    let value = colonIndex === -1 ? '' : line.slice(colonIndex + 1)
    // 协议规定冒号后可以有一个可选空格，要去掉
    if (value.startsWith(' ')) value = value.slice(1)

    switch (field) {
      case 'event':
        this.eventName = value
        break
      case 'data':
        this.dataLines.push(value)
        break
      case 'id':
        this.lastEventId = value
        break
      // retry 会改变重连间隔。我们用自己的退避策略，因此忽略它。
      default:
        break
    }

    return null
  }
}
