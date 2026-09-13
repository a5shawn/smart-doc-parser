import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { tasksApi, type TaskOut, type TaskStatus } from '@/api/client'
import { createSseConnection, type SseConnection, type SseState } from '@/api/sse'
import type { SseEvent } from '@/utils/sseParse'

/** SSE `chunk` 事件的载荷。 */
interface ChunkPayload {
  task_id: string
  kind: 'reasoning' | 'content' | 'reset'
  text: string
}

export const useTasksStore = defineStore('tasks', () => {
  /** 任务表。用对象而不是 Map——对象的响应式追踪更直观，调试时也更好看。 */
  const tasksById = ref<Record<string, TaskOut>>({})
  /** 每个任务已累积的模型正文（流式）。 */
  const streamText = ref<Record<string, string>>({})
  /** 每个任务已累积的模型思考过程。 */
  const reasoningText = ref<Record<string, string>>({})
  const connectionState = ref<SseState>('closed')
  /** 最近一次提交的批次，用于列表页只展示本批。 */
  const activeBatchId = ref<string | null>(null)

  let connection: SseConnection | null = null

  const allTasks = computed(() =>
    Object.values(tasksById.value).sort(
      (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
    ),
  )

  const activeTasks = computed(() =>
    allTasks.value.filter((task) => task.status !== 'completed' && task.status !== 'failed'),
  )

  function byBatch(batchId: string | null): TaskOut[] {
    if (!batchId) return allTasks.value
    return allTasks.value.filter((task) => task.batch_id === batchId)
  }

  function upsert(task: TaskOut): void {
    tasksById.value[task.id] = task
  }

  function upsertMany(tasks: TaskOut[]): void {
    for (const task of tasks) upsert(task)
  }

  function clearStream(taskId: string): void {
    delete streamText.value[taskId]
    delete reasoningText.value[taskId]
  }

  function applyChunk(payload: ChunkPayload): void {
    const { task_id: taskId, kind, text } = payload

    // 输出被截断重跑：丢弃已累积的内容。
    // 不处理的话，界面会把两次输出的文本拼在一起，
    // 显示出一段看着合法、实则错乱的 JSON。
    if (kind === 'reset') {
      clearStream(taskId)
      return
    }

    if (kind === 'reasoning') {
      reasoningText.value[taskId] = (reasoningText.value[taskId] ?? '') + text
    } else {
      streamText.value[taskId] = (streamText.value[taskId] ?? '') + text
    }
  }

  function handleEvent(event: SseEvent): void {
    if (!event.data) return

    let payload: unknown
    try {
      payload = JSON.parse(event.data)
    } catch {
      console.warn('[sse] 无法解析事件载荷', event)
      return
    }

    switch (event.event) {
      case 'snapshot':
        upsertMany((payload as { tasks: TaskOut[] }).tasks)
        break
      case 'progress':
      case 'done':
        upsert(payload as TaskOut)
        break
      case 'chunk':
        applyChunk(payload as ChunkPayload)
        break
      default:
        break
    }
  }

  /**
   * 建立进度流。
   *
   * **全局只开一条连接**。批量 20 个文件时，如果每个任务开一条 SSE，
   * 会直接撞上浏览器同源连接数上限（HTTP/1.1 下约 6 个），
   * 连普通请求都发不出去。事件里带 task_id，前端自己分发。
   */
  function connect(): void {
    if (connection) return

    connection = createSseConnection({
      path: '/tasks/stream',
      onEvent: handleEvent,
      onStateChange: (state) => {
        connectionState.value = state
      },
    })
  }

  function disconnect(): void {
    connection?.close()
    connection = null
    connectionState.value = 'closed'
  }

  async function fetchTask(taskId: string): Promise<TaskOut> {
    const task = await tasksApi.detail(taskId)
    upsert(task)
    return task
  }

  /** 从服务端拉取任务列表，用于首屏与断线恢复。 */
  async function loadRecent(
    params: {
      page?: number
      page_size?: number
      status?: TaskStatus
      batch_id?: string
    } = {},
  ): Promise<TaskOut[]> {
    const page = await tasksApi.list(params)
    upsertMany(page.items)
    return page.items
  }

  async function submit(
    documentIds: string[],
    templateKey: string,
  ): Promise<{ batchId: string; tasks: TaskOut[] }> {
    const response = await tasksApi.create({
      document_ids: documentIds,
      template_key: templateKey,
    })

    activeBatchId.value = response.batch_id
    upsertMany(response.tasks)
    for (const task of response.tasks) clearStream(task.id)
    return { batchId: response.batch_id, tasks: response.tasks }
  }

  async function retry(taskId: string): Promise<void> {
    const task = await tasksApi.retry(taskId)
    upsert(task)
    clearStream(taskId)
  }

  async function remove(taskId: string): Promise<void> {
    await tasksApi.remove(taskId)
    delete tasksById.value[taskId]
    clearStream(taskId)
  }

  return {
    tasksById,
    streamText,
    reasoningText,
    connectionState,
    activeBatchId,
    allTasks,
    activeTasks,
    byBatch,
    upsert,
    upsertMany,
    clearStream,
    handleEvent,
    connect,
    disconnect,
    fetchTask,
    loadRecent,
    submit,
    retry,
    remove,
  }
})
