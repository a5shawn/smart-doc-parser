<script setup lang="ts">
import { InboxOutlined } from '@ant-design/icons-vue'
import type { UploadFile } from 'ant-design-vue'
import { message } from 'ant-design-vue'
import { computed, ref } from 'vue'

import { documentsApi, type BatchUploadResponse } from '@/api/client'
import { ApiError } from '@/api/http'
import { useTasksStore } from '@/stores/tasks'
import { useTemplatesStore } from '@/stores/templates'

const emit = defineEmits<{ submitted: [batchId: string] }>()

const tasksStore = useTasksStore()
const templatesStore = useTemplatesStore()

const fileList = ref<UploadFile[]>([])
const selectedTemplate = ref<string>('')
const busy = ref(false)
const uploadPercent = ref(0)
const lastResult = ref<BatchUploadResponse | null>(null)

const acceptExtensions = '.pdf,.docx,.txt,.md'

const canSubmit = computed(
  () => fileList.value.length > 0 && selectedTemplate.value !== '' && !busy.value,
)

const templateOptions = computed(() =>
  templatesStore.templates.map((template) => ({
    value: template.key,
    label: template.builtin ? template.name : `${template.name}（自定义）`,
  })),
)

/** 阻止 antd 自动上传——我们要自己控制时机，跟模板选择一起提交。 */
function beforeUpload(): boolean {
  return false
}

function onRemove(file: UploadFile) {
  fileList.value = fileList.value.filter((item) => item.uid !== file.uid)
}

function reset() {
  fileList.value = []
  lastResult.value = null
  uploadPercent.value = 0
}

async function handleSubmit() {
  if (!canSubmit.value) return

  const files = fileList.value
    .map((item) => item.originFileObj as File | undefined)
    .filter((file): file is File => file !== undefined)

  if (files.length === 0) {
    message.warning('没有可上传的文件')
    return
  }

  busy.value = true
  uploadPercent.value = 0
  lastResult.value = null

  try {
    // 第一步：上传。后端采用部分成功语义，坏文件会出现在 rejected 里而不是抛异常
    const uploaded = await documentsApi.upload(files, (percent) => {
      uploadPercent.value = percent
    })
    lastResult.value = uploaded

    // 三个分组在生成的类型里都是可选的（后端定义带默认值），统一兜底成空数组
    const accepted = uploaded.accepted ?? []
    const duplicate = uploaded.duplicate ?? []
    const rejected = uploaded.rejected ?? []

    // 展示结果分类：新入库 / 已存在（内容相同）/ 被拒绝
    if (accepted.length > 0) {
      message.success(`${accepted.length} 个文件已上传`)
    }
    if (duplicate.length > 0) {
      message.info(`${duplicate.length} 个文件内容已存在，直接复用`)
    }
    for (const item of rejected) {
      message.warning(`${item.filename}：${item.message}`)
    }

    const documentIds = [...accepted, ...duplicate].map((doc) => doc.id)
    if (documentIds.length === 0) {
      return
    }

    // 第二步：建任务。接口立刻返回，进度通过 SSE 推过来
    const { batchId } = await tasksStore.submit(documentIds, selectedTemplate.value)
    message.success(`已提交 ${documentIds.length} 个抽取任务`)

    reset()
    emit('submitted', batchId)
  } catch (error) {
    if (error instanceof ApiError) {
      message.error(error.message)
    } else {
      message.error('提交失败，请重试')
    }
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <a-card :bordered="false" class="upload-panel">
    <a-upload-dragger
      v-model:file-list="fileList"
      :before-upload="beforeUpload"
      :multiple="true"
      :disabled="busy"
      :accept="acceptExtensions"
      @remove="onRemove"
    >
      <p class="ant-upload-drag-icon">
        <inbox-outlined />
      </p>
      <p class="ant-upload-text">点击或拖拽文件到此处</p>
      <p class="ant-upload-hint">支持 PDF / Word / 文本，可一次选多份。单个文件最大 20 MB。</p>
    </a-upload-dragger>

    <a-progress
      v-if="busy && uploadPercent > 0 && uploadPercent < 100"
      :percent="uploadPercent"
      size="small"
      style="margin-top: 12px"
    />

    <div class="upload-panel__actions">
      <a-space :size="12" wrap>
        <span class="upload-panel__label">抽取模板</span>
        <a-select
          v-model:value="selectedTemplate"
          :options="templateOptions"
          :loading="templatesStore.loading"
          placeholder="选择要抽取的字段模板"
          style="width: 240px"
          :disabled="busy"
        />
      </a-space>

      <a-button type="primary" :loading="busy" :disabled="!canSubmit" @click="handleSubmit">
        开始抽取
      </a-button>
    </div>

    <a-typography-text v-if="!templatesStore.loading && templateOptions.length === 0" type="danger">
      没有可用模板，请先检查后端连接（见「设置」页）。
    </a-typography-text>
  </a-card>
</template>

<style scoped>
.upload-panel__actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  margin-top: 16px;
}

.upload-panel__label {
  font-size: 13px;
  color: rgba(0, 0, 0, 0.65);
}
</style>
