<script setup lang="ts">
import { PlusOutlined } from '@ant-design/icons-vue'
import { message } from 'ant-design-vue'
import { computed, onMounted, ref, watch } from 'vue'

import { statsApi, tasksApi, type StatsOut, type TaskStatus } from '@/api/client'
import { ApiError } from '@/api/http'
import CustomTemplateModal from '@/components/CustomTemplateModal.vue'
import StatsCards from '@/components/StatsCards.vue'
import TaskTable from '@/components/TaskTable.vue'
import { useTasksStore } from '@/stores/tasks'
import { useTemplatesStore } from '@/stores/templates'

const tasksStore = useTasksStore()
const templatesStore = useTemplatesStore()

const stats = ref<StatsOut | null>(null)
const loading = ref(false)
const showTemplateModal = ref(false)

const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const statusFilter = ref<TaskStatus | undefined>(undefined)
const templateFilter = ref<string | undefined>(undefined)

/**
 * 历史页只展示**已经从服务端拉回来的**任务，而不是 store 里缓存的全部。
 *
 * 直接复用 store 会让这里受全局 SSE 快照影响——快照只包含进行中与最近完成的
 * 任务，翻到第 3 页时列表会莫名其妙地少掉一批。
 */
const rows = ref(tasksStore.allTasks)
const historyIds = ref<Set<string>>(new Set())

const filteredRows = computed(() => rows.value.filter((task) => historyIds.value.has(task.id)))

const statusOptions = [
  { value: 'completed', label: '已完成' },
  { value: 'failed', label: '失败' },
  { value: 'pending', label: '排队中' },
  { value: 'extracting', label: '抽取中' },
]

const templateOptions = computed(() =>
  templatesStore.templates.map((item) => ({ value: item.key, label: item.name })),
)

async function load() {
  loading.value = true
  try {
    const result = await tasksApi.list({
      page: page.value,
      page_size: pageSize.value,
      status: statusFilter.value,
      template_key: templateFilter.value,
    })
    total.value = result.total
    historyIds.value = new Set(result.items.map((task) => task.id))
    // 合并进 store，这样 SSE 推来的状态更新能实时反映到列表上
    tasksStore.upsertMany(result.items)
    rows.value = tasksStore.allTasks
  } catch (error) {
    message.error(error instanceof ApiError ? error.message : '加载失败')
  } finally {
    loading.value = false
  }
}

async function loadStats() {
  try {
    stats.value = await statsApi.get()
  } catch {
    // 统计失败不该阻塞主列表，静默降级即可
  }
}

async function refresh() {
  await Promise.all([load(), loadStats()])
}

watch([page, pageSize, statusFilter, templateFilter], () => {
  void load()
})

onMounted(async () => {
  await templatesStore.load()
  await refresh()
})
</script>

<template>
  <div class="sdp-page">
    <header class="sdp-page__header">
      <div class="history__header">
        <div>
          <h1 class="sdp-page__title">历史记录</h1>
          <p class="sdp-page__subtitle">
            全部抽取任务与用量统计。成本与 token 用量逐条记录，不是估算。
          </p>
        </div>
        <a-button @click="showTemplateModal = true">
          <template #icon><PlusOutlined /></template>
          自定义模板
        </a-button>
      </div>
    </header>

    <StatsCards :stats="stats" :loading="loading && !stats" />

    <a-card :bordered="false" class="history__tasks">
      <div class="history__filters">
        <a-space :size="12" wrap>
          <a-select
            v-model:value="statusFilter"
            :options="statusOptions"
            placeholder="全部状态"
            style="width: 150px"
            allow-clear
          />
          <a-select
            v-model:value="templateFilter"
            :options="templateOptions"
            placeholder="全部模板"
            style="width: 170px"
            allow-clear
          />
        </a-space>
      </div>

      <TaskTable :tasks="filteredRows" :loading="loading" @refresh="refresh" />

      <a-pagination
        v-if="total > pageSize"
        v-model:current="page"
        v-model:page-size="pageSize"
        :total="total"
        :show-size-changer="true"
        :page-size-options="[10, 20, 50]"
        :show-total="(count: number) => `共 ${count} 条`"
        class="history__pagination"
      />
    </a-card>

    <CustomTemplateModal v-model:open="showTemplateModal" />
  </div>
</template>

<style scoped>
.history__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.history__tasks {
  margin-top: 16px;
}

.history__filters {
  margin-bottom: 14px;
}

.history__pagination {
  margin-top: 16px;
  text-align: right;
}
</style>
