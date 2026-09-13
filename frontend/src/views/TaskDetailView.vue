<script setup lang="ts">
import { ArrowLeftOutlined, RedoOutlined } from '@ant-design/icons-vue'
import { message } from 'ant-design-vue'
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { tasksApi, type ExtractionResultOut, type TaskOut } from '@/api/client'
import { ApiError } from '@/api/http'
import JsonViewer from '@/components/JsonViewer.vue'
import ResultPanel from '@/components/ResultPanel.vue'
import StreamConsole from '@/components/StreamConsole.vue'
import TaskProgress from '@/components/TaskProgress.vue'
import { useTasksStore } from '@/stores/tasks'
import { useTemplatesStore } from '@/stores/templates'
import { formatCost, formatDateTime, formatDuration, formatNumber } from '@/utils/format'

const props = defineProps<{ id: string }>()

const router = useRouter()
const tasksStore = useTasksStore()
const templatesStore = useTemplatesStore()

const task = ref<TaskOut | null>(null)
const result = ref<ExtractionResultOut | null>(null)
const loading = ref(true)
const resultError = ref('')
const activeTab = ref('result')

const template = computed(() =>
  task.value ? templatesStore.findByKey(task.value.template_key) : undefined,
)

const streamContent = computed(() => tasksStore.streamText[props.id] ?? '')
const streamReasoning = computed(() => tasksStore.reasoningText[props.id] ?? '')

const isRunning = computed(
  () => task.value !== null && task.value.status !== 'completed' && task.value.status !== 'failed',
)

async function loadTask() {
  task.value = await tasksStore.fetchTask(props.id)
}

async function loadResult() {
  if (!task.value) return

  if (task.value.status === 'failed') {
    resultError.value = ''
    result.value = null
    return
  }

  try {
    result.value = await tasksApi.result(props.id)
    resultError.value = ''
  } catch (error) {
    resultError.value = error instanceof ApiError ? error.message : '结果加载失败'
  }
}

async function refresh() {
  loading.value = true
  try {
    await loadTask()
    await loadResult()
  } catch (error) {
    message.error(error instanceof ApiError ? error.message : '加载失败')
  } finally {
    loading.value = false
  }
}

async function handleRetry() {
  try {
    await tasksStore.retry(props.id)
    message.success('已重新提交抽取')
    await refresh()
  } catch (error) {
    message.error(error instanceof ApiError ? error.message : '重试失败')
  }
}

// 任务进入终态时自动拉取结果——用户不需要手动刷新
watch(
  () => task.value?.status,
  async (status, previous) => {
    if (status === 'completed' && previous !== 'completed') {
      await loadResult()
    }
  },
)

onMounted(async () => {
  await templatesStore.load()
  await refresh()
})
</script>

<template>
  <div class="sdp-page">
    <a-space :size="12" class="detail__back">
      <a-button type="text" @click="router.push('/')">
        <template #icon><ArrowLeftOutlined /></template>
        返回工作台
      </a-button>
    </a-space>

    <a-card :bordered="false" :loading="loading">
      <template #title>
        <span>任务详情</span>
        <a-typography-text code class="detail__id">{{ id }}</a-typography-text>
      </template>

      <template #extra>
        <a-button v-if="task && !isRunning" size="small" @click="handleRetry">
          <template #icon><RedoOutlined /></template>
          重新抽取
        </a-button>
      </template>

      <template v-if="task">
        <a-descriptions :column="{ xs: 1, sm: 2, md: 3 }" size="small" bordered>
          <a-descriptions-item label="来源文档">
            {{ task.document_filename ?? '（文档已删除）' }}
          </a-descriptions-item>
          <a-descriptions-item label="抽取模板">
            {{ task.template_name }}
          </a-descriptions-item>
          <a-descriptions-item label="模型">
            {{ task.model ?? '—' }}
          </a-descriptions-item>

          <a-descriptions-item label="耗时">
            {{ formatDuration(task.latency_ms) }}
          </a-descriptions-item>
          <a-descriptions-item label="成本">
            <a-tooltip
              :title="`${formatNumber(task.prompt_tokens)} 输入 + ${formatNumber(task.completion_tokens)} 输出 token`"
            >
              {{ formatCost(task.cost_usd) }}
            </a-tooltip>
          </a-descriptions-item>
          <a-descriptions-item label="创建时间">
            {{ formatDateTime(task.created_at) }}
          </a-descriptions-item>

          <a-descriptions-item label="思考 token">
            {{ formatNumber(task.reasoning_tokens) }}
            <span class="sdp-muted">（已含在输出内）</span>
          </a-descriptions-item>
          <a-descriptions-item label="缓存命中">
            {{ formatNumber(task.cached_tokens) }}
          </a-descriptions-item>
          <a-descriptions-item label="尝试次数"> 第 {{ task.attempts }} 次 </a-descriptions-item>
        </a-descriptions>

        <div class="detail__progress">
          <TaskProgress :task="task" />
        </div>
      </template>
    </a-card>

    <a-card :bordered="false" class="detail__content">
      <a-tabs v-model:active-key="activeTab">
        <a-tab-pane key="result" tab="结构化结果">
          <a-result
            v-if="!isRunning && !result && task?.status === 'failed'"
            status="warning"
            title="本次抽取没有结果"
            :sub-title="task.error_message ?? '任务执行失败'"
          />

          <a-spin v-else-if="isRunning" tip="抽取进行中，完成后会自动展示结果…">
            <div class="detail__placeholder" />
          </a-spin>

          <a-alert v-else-if="resultError" type="info" show-icon :message="resultError" />

          <ResultPanel v-else-if="result" :result="result" :template="template" :task-id="id" />
        </a-tab-pane>

        <a-tab-pane key="raw" tab="原始输出">
          <JsonViewer
            v-if="result?.raw_output"
            :data="result.raw_output"
            :filename="`task-${id}-raw.json`"
          />
          <a-empty v-else description="暂无原始输出" />
        </a-tab-pane>

        <a-tab-pane key="stream" tab="流式输出">
          <StreamConsole
            :content="streamContent"
            :reasoning="streamReasoning"
            :running="isRunning"
          />
          <a-alert
            v-if="!streamContent && !streamReasoning && !isRunning"
            type="info"
            show-icon
            class="detail__stream-hint"
            message="没有流式记录"
            description="流式分片走的是进程内广播通道，页面在任务开始后才打开、或服务跑在多个 worker 上时收不到。这不影响抽取结果——结果本身始终以数据库为准。"
          />
        </a-tab-pane>
      </a-tabs>
    </a-card>
  </div>
</template>

<style scoped>
.detail__back {
  margin-bottom: 8px;
}

.detail__id {
  margin-left: 10px;
  font-size: 12px;
  font-weight: 400;
}

.detail__progress {
  margin-top: 16px;
}

.detail__content {
  margin-top: 16px;
}

.detail__placeholder {
  height: 160px;
}

.detail__stream-hint {
  margin-top: 12px;
}
</style>
