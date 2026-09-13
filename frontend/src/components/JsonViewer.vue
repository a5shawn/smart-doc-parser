<script setup lang="ts">
import { CopyOutlined, DownloadOutlined } from '@ant-design/icons-vue'
import { message } from 'ant-design-vue'
import { computed } from 'vue'

import { copyText, downloadJson } from '@/utils/download'

const props = defineProps<{
  data: unknown
  /** 下载时的文件名 */
  filename?: string
}>()

const text = computed(() =>
  typeof props.data === 'string' ? props.data : JSON.stringify(props.data, null, 2),
)

async function handleCopy() {
  const ok = await copyText(text.value)
  if (ok) {
    message.success('已复制到剪贴板')
  } else {
    message.error('复制失败，请手动选择复制')
  }
}

function handleDownload() {
  downloadJson(props.data, props.filename ?? 'result.json')
}
</script>

<template>
  <div>
    <div class="json-viewer__toolbar">
      <a-space :size="8">
        <a-button size="small" @click="handleCopy">
          <template #icon><CopyOutlined /></template>
          复制
        </a-button>
        <a-button size="small" @click="handleDownload">
          <template #icon><DownloadOutlined /></template>
          下载 JSON
        </a-button>
      </a-space>
    </div>
    <pre class="sdp-json">{{ text }}</pre>
  </div>
</template>

<style scoped>
.json-viewer__toolbar {
  margin-bottom: 8px;
}
</style>
