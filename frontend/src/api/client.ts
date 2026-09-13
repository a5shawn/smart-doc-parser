/**
 * 带类型的接口封装。
 *
 * 类型全部来自 `types.gen.ts`（由后端 OpenAPI schema 自动生成）——
 * 后端改了字段，重新生成后前端会立刻在编译期报错，
 * 而不是等到线上发现某个字段读不到。
 */

import type { components } from './types.gen'
import { del, get, http, post } from './http'

export type DocumentOut = components['schemas']['DocumentOut']
export type DocumentDetailOut = components['schemas']['DocumentDetailOut']
export type BatchUploadResponse = components['schemas']['BatchUploadResponse']
export type DocumentDeleteResponse = components['schemas']['DocumentDeleteResponse']

export type TaskOut = components['schemas']['TaskOut']
export type TaskStatus = components['schemas']['TaskStatus']
export type TaskCreateRequest = components['schemas']['TaskCreateRequest']
export type TaskCreateResponse = components['schemas']['TaskCreateResponse']
export type TaskDeleteResponse = components['schemas']['TaskDeleteResponse']
export type ExtractionResultOut = components['schemas']['ExtractionResultOut']
export type TaskWarningOut = components['schemas']['TaskWarningOut']

export type TemplateOut = components['schemas']['TemplateOut']
export type TemplateFieldOut = components['schemas']['TemplateFieldOut']
export type CustomTemplateCreate = components['schemas']['CustomTemplateCreate']
export type CustomTemplateOut = components['schemas']['CustomTemplateOut']
export type TemplateDeleteResponse = components['schemas']['TemplateDeleteResponse']

export type StatsOut = components['schemas']['StatsOut']

/** 后端统一的分页信封。 */
export interface Page<T> {
  items: T[]
  total: number
  page: number
  page_size: number
  pages: number
}

// ============================================================================
// 文档
// ============================================================================
export const documentsApi = {
  /**
   * 上传 1~N 个文件。
   *
   * 后端采用「部分成功」语义：单个文件失败不会让整批回滚，
   * 失败项会出现在 rejected 里。因此这里**不抛异常**，把结果原样交给调用方展示。
   */
  async upload(
    files: File[],
    onProgress?: (percent: number) => void,
  ): Promise<BatchUploadResponse> {
    const form = new FormData()
    for (const file of files) {
      form.append('files', file)
    }

    const response = await http.post<BatchUploadResponse>('/documents', form, {
      onUploadProgress: (event) => {
        if (onProgress && event.total) {
          onProgress(Math.round((event.loaded / event.total) * 100))
        }
      },
    })
    return response.data
  },

  list: (page = 1, pageSize = 20) =>
    get<Page<DocumentOut>>('/documents', { params: { page, page_size: pageSize } }),

  detail: (id: string) => get<DocumentDetailOut>(`/documents/${id}`),

  remove: (id: string) => del<DocumentDeleteResponse>(`/documents/${id}`),
}

// ============================================================================
// 抽取任务
// ============================================================================
export const tasksApi = {
  create: (payload: TaskCreateRequest) => post<TaskCreateResponse>('/tasks', payload),

  list: (params: {
    page?: number
    page_size?: number
    status?: TaskStatus
    template_key?: string
    batch_id?: string
  }) => get<Page<TaskOut>>('/tasks', { params }),

  detail: (id: string) => get<TaskOut>(`/tasks/${id}`),

  result: (id: string) => get<ExtractionResultOut>(`/tasks/${id}/result`),

  retry: (id: string) => post<TaskOut>(`/tasks/${id}/retry`),

  remove: (id: string) => del<TaskDeleteResponse>(`/tasks/${id}`),
}

// ============================================================================
// 模板
// ============================================================================
export const templatesApi = {
  list: () => get<TemplateOut[]>('/templates'),

  create: (payload: CustomTemplateCreate) => post<CustomTemplateOut>('/templates', payload),

  remove: (id: string) => del<TemplateDeleteResponse>(`/templates/${id}`),
}

// ============================================================================
// 统计与健康
// ============================================================================
export const statsApi = {
  get: () => get<StatsOut>('/stats'),
}

export const healthApi = {
  live: () =>
    get<{ status: string; app: string; version: string; environment: string }>('/health/live'),
  ready: () => get<{ status: string; database: string }>('/health/ready'),
}
