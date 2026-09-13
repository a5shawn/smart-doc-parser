<script setup lang="ts">
import { PlusOutlined } from '@ant-design/icons-vue'
import { computed, onMounted, ref } from 'vue'

import CustomTemplateModal from '@/components/CustomTemplateModal.vue'
import TaskTable from '@/components/TaskTable.vue'
import UploadPanel from '@/components/UploadPanel.vue'
import { useTasksStore } from '@/stores/tasks'

const tasksStore = useTasksStore()

const showTemplateModal = ref(false)
const loadingRecent = ref(false)
const currentBatchId = ref<string | null>(null)

/**
 * 优先展示本批任务；没有本批时展示最近的进行中任务。
 *
 * 不做"只展示本批"的硬过滤：用户刷新页面后 activeBatchId 就没了，
 * 那时应该还能看到后台正在跑的任务，而不是一片空白。
 */
const visibleTasks = computed(() => {
  const batchTasks = tasksStore.byBatch(currentBatchId.value)
  if (currentBatchId.value && batchTasks.length > 0) return batchTasks
  return tasksStore.allTasks.slice(0, 20)
})

const hasActiveTasks = computed(() => tasksStore.activeTasks.length > 0)

async function loadRecent() {
  loadingRecent.value = true
  try {
    await tasksStore.loadRecent({ page: 1, page_size: 20 })
  } finally {
    loadingRecent.value = false
  }
}

function onSubmitted(batchId: string) {
  currentBatchId.value = batchId
}

onMounted(() => {
  void loadRecent()
})
</script>

<template>
  <div class="sdp-page">
    <header class="sdp-page__header">
      <div class="extract-view__header">
        <div>
          <h1 class="sdp-page__title">抽取工作台</h1>
          <p class="sdp-page__subtitle">
            上传文档 → 选择模板 → 大模型抽取成结构化 JSON。进度实时推送，无需刷新页面。
          </p>
        </div>
        <a-button @click="showTemplateModal = true">
          <template #icon><PlusOutlined /></template>
          自定义模板
        </a-button>
      </div>
    </header>

    <UploadPanel @submitted="onSubmitted" />

    <a-card :bordered="false" class="extract-view__tasks">
      <template #title>
        <a-space :size="8">
          <span>{{ currentBatchId ? '本批任务' : '最近任务' }}</span>
          <a-tag v-if="hasActiveTasks" color="processing">
            {{ tasksStore.activeTasks.length }} 个进行中
          </a-tag>
        </a-space>
      </template>

      <TaskTable :tasks="visibleTasks" :loading="loadingRecent" @refresh="loadRecent" />
    </a-card>

    <CustomTemplateModal v-model:open="showTemplateModal" />
  </div>
</template>

<style scoped>
.extract-view__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.extract-view__tasks {
  margin-top: 16px;
}
</style>
