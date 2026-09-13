<script setup lang="ts">
import { computed } from 'vue'

import type { TaskOut, TaskStatus } from '@/api/client'

const props = defineProps<{
  task: TaskOut
  /** 紧凑模式：表格里只显示标签，不显示进度条与阶段说明 */
  compact?: boolean
}>()

/** 状态标签的文案与配色。 */
const STATUS_META: Record<TaskStatus, { text: string; color: string }> = {
  pending: { text: '排队中', color: 'default' },
  parsing: { text: '解析文档', color: 'processing' },
  extracting: { text: '模型抽取中', color: 'processing' },
  validating: { text: '校验结果', color: 'processing' },
  completed: { text: '已完成', color: 'success' },
  failed: { text: '失败', color: 'error' },
}

const meta = computed(
  () => STATUS_META[props.task.status] ?? { text: props.task.status, color: 'default' },
)

const isRunning = computed(
  () => props.task.status !== 'completed' && props.task.status !== 'failed',
)

/**
 * 扫描件的提示要按「警告」而不是「错误」呈现。
 *
 * 这不是系统故障，而是文档本身的形态问题——用户换一份电子版就能解决。
 * 红色的报错会让用户以为是自己操作错了或者系统坏了。
 */
const isNoTextLayer = computed(() => props.task.error_code === 'PDF_NO_TEXT_LAYER')

const progressStatus = computed(() => {
  if (props.task.status === 'failed') return 'exception'
  if (props.task.status === 'completed') return 'success'
  return 'active'
})
</script>

<template>
  <div class="task-progress">
    <a-space :size="6" wrap>
      <a-tag :color="meta.color">{{ meta.text }}</a-tag>
      <a-tag v-if="task.template_name" color="blue">{{ task.template_name }}</a-tag>
      <a-tooltip v-if="task.attempts > 1" title="包含失败重试的次数">
        <a-tag>第 {{ task.attempts }} 次尝试</a-tag>
      </a-tooltip>
    </a-space>

    <template v-if="!compact">
      <a-progress
        class="task-progress__bar"
        :percent="task.progress"
        :status="progressStatus"
        :stroke-color="isRunning ? undefined : task.status === 'completed' ? '#52c41a' : '#ff4d4f'"
        size="small"
      />

      <div v-if="isRunning && task.stage_message" class="task-progress__stage">
        {{ task.stage_message }}
      </div>
    </template>

    <a-alert
      v-if="task.error_message"
      class="task-progress__error"
      :type="isNoTextLayer ? 'warning' : 'error'"
      :message="isNoTextLayer ? '无法抽取：文档没有文本层' : '处理失败'"
      :description="task.error_message"
      show-icon
      :closable="false"
    />
  </div>
</template>

<style scoped>
.task-progress__bar {
  margin-top: 10px;
  margin-bottom: 0;
}

.task-progress__stage {
  font-size: 12px;
  color: rgba(0, 0, 0, 0.45);
  margin-top: 2px;
}

.task-progress__error {
  margin-top: 10px;
  font-size: 12px;
}
</style>
