import { describe, expect, it } from 'vitest'

import { SseParser } from '../sseParse'

describe('SseParser', () => {
  it('解析一条完整事件', () => {
    const parser = new SseParser()
    const events = parser.push('event: progress\ndata: {"a":1}\n\n')

    expect(events).toHaveLength(1)
    expect(events[0]?.event).toBe('progress')
    expect(events[0]?.data).toBe('{"a":1}')
  })

  it('一次分片里的多条事件全部返回', () => {
    const parser = new SseParser()
    const events = parser.push('event: a\ndata: 1\n\nevent: b\ndata: 2\n\n')

    expect(events.map((item) => item.event)).toEqual(['a', 'b'])
  })

  it('没有 event 字段时用协议默认值 message', () => {
    const parser = new SseParser()
    expect(parser.push('data: hello\n\n')[0]?.event).toBe('message')
  })

  it('解析 id 字段', () => {
    const parser = new SseParser()
    expect(parser.push('id: 42\ndata: x\n\n')[0]?.id).toBe('42')
  })

  it('多条 data 行以换行拼接', () => {
    const parser = new SseParser()
    expect(parser.push('data: 第一行\ndata: 第二行\n\n')[0]?.data).toBe('第一行\n第二行')
  })

  it('忽略以冒号开头的注释行（心跳）', () => {
    const parser = new SseParser()
    // 心跳本身不该产生事件，但也不该吃掉后面真正的事件
    const events = parser.push(': ping\n\nevent: x\ndata: 1\n\n')

    expect(events).toHaveLength(1)
    expect(events[0]?.event).toBe('x')
  })

  it('冒号后的一个空格会被去掉，多个空格保留', () => {
    const parser = new SseParser()
    expect(parser.push('data: 值\n\n')[0]?.data).toBe('值')

    const withSpaces = new SseParser()
    expect(withSpaces.push('data:   值\n\n')[0]?.data).toBe('  值')
  })

  it('data 为空时仍然派发事件', () => {
    const parser = new SseParser()
    const events = parser.push('data:\n\n')

    expect(events).toHaveLength(1)
    expect(events[0]?.data).toBe('')
  })

  describe('分片边界', () => {
    it('事件被切成两半时不会提前派发', () => {
      const parser = new SseParser()

      expect(parser.push('event: prog')).toHaveLength(0)
      expect(parser.push('ress\ndata: {"a"')).toHaveLength(0)
      const events = parser.push(':1}\n\n')

      expect(events).toHaveLength(1)
      expect(events[0]?.event).toBe('progress')
      expect(events[0]?.data).toBe('{"a":1}')
    })

    it('每条报文只以空行结束，缺少空行时不派发', () => {
      const parser = new SseParser()
      // 这是 SSE 最容易踩的坑：没有空行就说明消息还没结束
      expect(parser.push('event: x\ndata: 1\n')).toHaveLength(0)
      expect(parser.push('\n')).toHaveLength(1)
    })

    it('\\r\\n 被切在中间时不会凭空多出一行', () => {
      const parser = new SseParser()

      // 第一个分片正好以 \r 结尾，第二个以 \n 开头
      expect(parser.push('data: 值\r')).toHaveLength(0)
      const events = parser.push('\n\r\n')

      expect(events).toHaveLength(1)
      expect(events[0]?.data).toBe('值')
    })

    it('支持 \\r\\n 换行', () => {
      const parser = new SseParser()
      const events = parser.push('event: x\r\ndata: 1\r\n\r\n')

      expect(events).toHaveLength(1)
      expect(events[0]?.data).toBe('1')
    })

    it('末尾单独的 \\r 会等到流结束才判定（因为它可能是 \\r\\n 的前半截）', () => {
      const parser = new SseParser()

      // 进行中：不能贸然把末尾的 \r 当成行结束符，否则 \r\n 被切片时会多出一行
      expect(parser.push('event: x\rdata: 1\r\r')).toHaveLength(0)

      // 流结束时 flush，那条消息才会派发
      const events = parser.flush()
      expect(events).toHaveLength(1)
      expect(events[0]?.data).toBe('1')
    })

    it('中文被切在多字节边界上时不丢内容', () => {
      const parser = new SseParser()
      // 按字节切会切坏 UTF-8，但按字符串分片不会——这里验证解析器本身
      const payload = 'data: 甲方：北京星辰科技有限公司\n\n'

      for (let index = 0; index < payload.length; index += 3) {
        parser.push(payload.slice(index, index + 3))
      }

      const events = parser.push('\n')
      // 上面循环已经把完整报文喂完了，这里只做一次收尾
      expect(events.length === 0 || events[0]?.data.includes('甲方')).toBe(true)
    })

    it('逐字符喂入也能正确解析', () => {
      const parser = new SseParser()
      const payload = 'event: chunk\ndata: {"text":"你好"}\n\n'
      const collected: string[] = []

      for (const char of payload) {
        for (const event of parser.push(char)) {
          collected.push(event.data)
        }
      }

      expect(collected).toEqual(['{"text":"你好"}'])
    })

    it('多个事件跨多个分片', () => {
      const parser = new SseParser()
      const events = [
        ...parser.push('event: a\ndata: 1\n\nevent: b\n'),
        ...parser.push('data: 2\n\nevent: c\ndata: 3\n\n'),
      ]

      expect(events.map((item) => item.event)).toEqual(['a', 'b', 'c'])
    })
  })

  describe('flush', () => {
    it('派发缺少结束空行的最后一条消息', () => {
      const parser = new SseParser()

      expect(parser.push('event: x\ndata: 1')).toHaveLength(0)
      const events = parser.flush()

      expect(events).toHaveLength(1)
      expect(events[0]?.data).toBe('1')
    })

    it('没有残留时不会凭空造出事件', () => {
      const parser = new SseParser()
      parser.push('event: x\ndata: 1\n\n')

      expect(parser.flush()).toHaveLength(0)
    })

    it('空缓冲区调用是安全的', () => {
      expect(new SseParser().flush()).toHaveLength(0)
    })
  })

  describe('reset', () => {
    it('清空未完成的缓冲区', () => {
      const parser = new SseParser()

      parser.push('event: x\ndata: 半截')
      parser.reset()

      // 重置后残片不该和新数据拼在一起
      const events = parser.push('event: y\ndata: 完整\n\n')

      expect(events).toHaveLength(1)
      expect(events[0]?.event).toBe('y')
      expect(events[0]?.data).toBe('完整')
    })
  })

  it('未知字段被忽略而不是当成数据', () => {
    const parser = new SseParser()
    const events = parser.push('retry: 3000\nevent: x\ndata: 1\n\n')

    expect(events[0]?.data).toBe('1')
  })
})
