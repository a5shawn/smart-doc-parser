<script setup lang="ts">
import { computed, watch, ref } from 'vue'

const props = defineProps<{
  /** 已累积的模型正文（流式） */
  content: string
  /** 已累积的思考过程 */
  reasoning: string
  running?: boolean
}>()

const showReasoning = ref(false)
const contentRef = ref<HTMLElement | null>(null)

const hasReasoning = computed(() => props.reasoning.length > 0)
const hasContent = computed(() => props.content.length > 0)

// 自动滚到底部，让用户看到"正在写字"的最新进展
watch(
  () => props.content,
  () => {
    const element = contentRef.value
    if (element) {
      element.scrollTop = element.scrollHeight
    }
  },
)
</script>

<template>
  <div>
    <a-space v-if="running" :size="8" style="margin-bottom: 10px">
      <a-spin size="small" />
      <span class="sdp-muted">模型正在生成…（deepseek-flash 会先思考再输出，思考内容默认折叠）</span>
    </a-space>

    <!--
      思考过程默认折叠。推理模型的思考动辄上千字，默认展开会把真正要看的结果挤到屏幕外。
      但它必须可查——排查"模型为什么漏抽了这个字段"时，思考过程是最直接的线索。
    -->
    <div v-if="hasReasoning" class="stream-console__reasoning">
      <a-button type="link" size="small" @click="showReasoning = !showReasoning">
        {{ showReasoning ? '收起思考过程' : `展开思考过程（${reasoning.length} 字）` }}
      </a-button>
      <pre v-if="showReasoning" class="sdp-stream sdp-stream--reasoning">{{ reasoning }}</pre>
    </div>

    <pre v-if="hasContent" ref="contentRef" class="sdp-stream">{{ content }}</pre>

    <a-empty
      v-if="!hasContent && !hasReasoning"
      description="等待模型输出…"
      :image-style="{ height: '48px' }"
    />
  </div>
</template>

<style scoped>
.stream-console__reasoning {
  margin-bottom: 8px;
}
</style>
