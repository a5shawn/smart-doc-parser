<script setup lang="ts">
import { DeleteOutlined, EyeOutlined, RedoOutlined, ReloadOutlined } from '@ant-design/icons-vue'
import { message, Modal } from 'ant-design-vue'
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import type { TaskOut } from '@/api/client'
import { ApiError } from '@/api/http'
import { useTasksStore } from '@/stores/tasks'
import { formatCost, formatDuration, formatRelative } from '@/utils/format'

import TaskProgress from './TaskProgress.vue'

const props = defineProps<{
  tasks: TaskOut[]
  loading?: boolean
  /** 是否显示"查看详情"以外的操作按钮 */
  showActions?: boolean
}>()

const emit = defineEmits<{ refresh: [] }>()

const router = useRouter()
const tasksStore = useTasksStore()

const columns = [
  { title: '文档', dataIndex: 'document_filename', key: 'document_filename', ellipsis: true },
  { title: '状态', key: 'status', width: 260 },
  { title: '模板', dataIndex: 'template_name', key: 'template_name', width: 100 },
  { title: '耗时', key: 'latency_ms', width: 100 },
  { title: '成本', key: 'cost_usd', width: 100 },
  { title: '创建时间', key: 'created_at', width: 120 },
  { title: '操作', key: 'actions', width: 150, fixed: 'right' as const },
]

const isTerminal = (task: TaskOut) => task.status === 'completed' || task.status === 'failed'

const hasRunning = computed(() => props.tasks.some((task) => !isTerminal(task)))

function openDetail(task: TaskOut) {
  void router.push({ name: 'task-detail', params: { id: task.id } })
}

async function handleRetry(task: TaskOut) {
  try {
    await tasksStore.retry(task.id)
    message.success('已重新提交抽取')
  } catch (error) {
    message.error(error instanceof ApiError ? error.message : '重试失败')
  }
}

async function handleDelete(task: TaskOut) {
  Modal.confirm({
    title: '删除这个抽取任务？',
    content: `任务「${task.document_filename ?? task.id}」的抽取结果会一并删除，且不可恢复。`,
    okText: '删除',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      try {
        await tasksStore.remove(task.id)
        message.success('已删除')
      } catch (error) {
        message.error(error instanceof ApiError ? error.message : '删除失败')
      }
    },
  })
}
</script>

<template>
  <div>
    <div class="task-table__toolbar">
      <a-space :size="8">
        <span class="sdp-muted">共 {{ tasks.length }} 个任务</span>
        <a-tag v-if="hasRunning" color="processing">进行中</a-tag>
      </a-space>
      <a-button size="small" :loading="loading" @click="emit('refresh')">
        <template #icon><ReloadOutlined /></template>
        刷新
      </a-button>
    </div>

    <a-table
      :columns="columns"
      :data-source="tasks"
      :loading="loading"
      :pagination="false"
      row-key="id"
      size="middle"
      :scroll="{ x: 900 }"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'document_filename'">
          <a-typography-text :ellipsis="{ tooltip: record.document_filename }">
            {{ record.document_filename ?? '（文档已删除）' }}
          </a-typography-text>
        </template>

        <template v-else-if="column.key === 'status'">
          <TaskProgress :task="record as TaskOut" compact />
        </template>

        <template v-else-if="column.key === 'latency_ms'">
          <span>{{ formatDuration(record.latency_ms) }}</span>
        </template>

        <template v-else-if="column.key === 'cost_usd'">
          <a-tooltip
            :title="`${record.prompt_tokens} 输入 + ${record.completion_tokens} 输出 token（含 ${record.reasoning_tokens} 思考）`"
          >
            <span>{{ formatCost(record.cost_usd) }}</span>
          </a-tooltip>
        </template>

        <template v-else-if="column.key === 'created_at'">
          <span class="sdp-muted">{{ formatRelative(record.created_at) }}</span>
        </template>

        <template v-else-if="column.key === 'actions'">
          <a-space :size="2">
            <a-tooltip title="查看详情">
              <a-button type="text" size="small" @click="openDetail(record as TaskOut)">
                <template #icon><EyeOutlined /></template>
              </a-button>
            </a-tooltip>
            <a-tooltip v-if="showActions !== false" title="重新抽取">
              <a-button
                type="text"
                size="small"
                :disabled="!isTerminal(record as TaskOut)"
                @click="handleRetry(record as TaskOut)"
              >
                <template #icon><RedoOutlined /></template>
              </a-button>
            </a-tooltip>
            <a-tooltip v-if="showActions !== false" title="删除">
              <a-button type="text" size="small" danger @click="handleDelete(record as TaskOut)">
                <template #icon><DeleteOutlined /></template>
              </a-button>
            </a-tooltip>
          </a-space>
        </template>
      </template>
    </a-table>
  </div>
</template>

<style scoped>
.task-table__toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
</style>
