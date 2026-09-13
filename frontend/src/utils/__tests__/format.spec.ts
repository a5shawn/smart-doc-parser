import { describe, expect, it } from 'vitest'

import {
  formatBytes,
  formatCost,
  formatDuration,
  formatFieldValue,
  formatNumber,
  isObjectArray,
} from '../format'

describe('formatCost', () => {
  it('单份文档的成本保留足够小数位', () => {
    // 一份合同的成本约 ¥0.005，两位小数会显示成 ¥0.01 甚至 ¥0.00，失去意义
    const text = formatCost(0.0007)
    expect(text).not.toBe('¥0.00')
    expect(text.startsWith('¥')).toBe(true)
  })

  it('较大金额用两位小数', () => {
    expect(formatCost(1)).toBe('¥7.10')
  })

  it('零和空值不崩', () => {
    expect(formatCost(0)).toBe('¥0')
    expect(formatCost(null)).toBe('¥0')
    expect(formatCost(undefined)).toBe('¥0')
  })
})

describe('formatBytes', () => {
  it.each([
    [0, '0 B'],
    [512, '512 B'],
    [1024, '1.0 KB'],
    [1536, '1.5 KB'],
    [1024 * 1024, '1.0 MB'],
  ])('%i 字节 → %s', (input, expected) => {
    expect(formatBytes(input)).toBe(expected)
  })

  it('空值不崩', () => {
    expect(formatBytes(null)).toBe('0 B')
  })
})

describe('formatDuration', () => {
  it('毫秒内显示毫秒', () => {
    expect(formatDuration(350)).toBe('350 ms')
  })

  it('超过一秒显示秒', () => {
    expect(formatDuration(1500)).toBe('1.50 s')
  })

  it('空值显示占位符', () => {
    expect(formatDuration(null)).toBe('—')
    expect(formatDuration(undefined)).toBe('—')
  })
})

describe('formatNumber', () => {
  it('加千分位', () => {
    expect(formatNumber(1280000)).toBe('1,280,000')
  })

  it('空值当零处理', () => {
    expect(formatNumber(null)).toBe('0')
  })
})

describe('formatFieldValue', () => {
  it('空值返回空串', () => {
    expect(formatFieldValue(null)).toBe('')
    expect(formatFieldValue(undefined)).toBe('')
  })

  it('布尔值渲染成是/否', () => {
    expect(formatFieldValue(true)).toBe('是')
    expect(formatFieldValue(false)).toBe('否')
  })

  it('数字 0 是有效值，不能被当成空', () => {
    // 用 !value 判断的话，"金额是 0" 会显示成"未抽取到"，是实打实的错误
    expect(formatFieldValue(0)).toBe('0')
  })

  it('布尔 false 是有效值', () => {
    expect(formatFieldValue(false)).toBe('否')
  })

  it('字符串原样返回', () => {
    expect(formatFieldValue('北京星辰科技有限公司')).toBe('北京星辰科技有限公司')
  })

  it('数组序列化', () => {
    expect(formatFieldValue(['Vue', 'Python'])).toBe('["Vue","Python"]')
  })

  it('空数组返回空串（视为未抽取）', () => {
    expect(formatFieldValue([])).toBe('')
  })

  it('嵌套对象序列化', () => {
    expect(formatFieldValue({ a: 1 })).toBe('{"a":1}')
  })
})

describe('isObjectArray', () => {
  it('对象数组返回 true', () => {
    expect(isObjectArray([{ a: 1 }, { b: 2 }])).toBe(true)
  })

  it('字符串数组返回 false', () => {
    expect(isObjectArray(['a', 'b'])).toBe(false)
  })

  it('空数组返回 false（无法判断元素类型，按标签渲染更安全）', () => {
    expect(isObjectArray([])).toBe(false)
  })

  it('混合数组返回 false', () => {
    expect(isObjectArray([{ a: 1 }, 'text'])).toBe(false)
  })

  it('非数组返回 false', () => {
    expect(isObjectArray('text')).toBe(false)
    expect(isObjectArray(null)).toBe(false)
  })

  it('嵌套数组不算对象数组', () => {
    expect(isObjectArray([[1, 2]])).toBe(false)
  })
})
