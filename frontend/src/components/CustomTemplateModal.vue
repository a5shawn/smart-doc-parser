<script setup lang="ts">
import { message } from 'ant-design-vue'
import { computed, ref, watch } from 'vue'

import { ApiError } from '@/api/http'
import { useTemplatesStore } from '@/stores/templates'

const open = defineModel<boolean>('open', { required: true })

const templatesStore = useTemplatesStore()

const name = ref('')
const description = ref('')
const promptHint = ref('')
const schemaText = ref('')
const submitting = ref(false)
const errorText = ref('')

/**
 * 预填一个能跑通的示例 schema。
 *
 * 空文本框会让用户卡在"我该写什么格式"上——给一个合法的起点，
 * 改起来比从零写快得多。
 */
const EXAMPLE_SCHEMA = JSON.stringify(
  {
    type: 'object',
    properties: {
      order_no: { type: 'string', title: '订单号', description: '采购单编号' },
      supplier: { type: 'string', title: '供应商' },
      total_amount: { type: 'number', title: '总金额', description: '只填数字' },
      order_date: { type: 'string', title: '下单日期', description: '格式 YYYY-MM-DD' },
    },
    required: ['order_no'],
  },
  null,
  2,
)

watch(open, (value) => {
  if (value) {
    errorText.value = ''
    if (!schemaText.value) schemaText.value = EXAMPLE_SCHEMA
  }
})

/** 在提交前先本地解析一次，把语法错误尽早暴露给用户。 */
const parsedSchema = computed<Record<string, unknown> | null>(() => {
  if (!schemaText.value.trim()) return null
  try {
    const parsed: unknown = JSON.parse(schemaText.value)
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return null
    }
    return parsed as Record<string, unknown>
  } catch {
    return null
  }
})

const canSubmit = computed(
  () => name.value.trim().length > 0 && parsedSchema.value !== null && !submitting.value,
)

function validate(): string {
  if (!name.value.trim()) return '请填写模板名称'
  if (!schemaText.value.trim()) return '请填写 JSON Schema'

  let parsed: unknown
  try {
    parsed = JSON.parse(schemaText.value)
  } catch (error) {
    return `JSON 语法错误：${error instanceof Error ? error.message : '无法解析'}`
  }

  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return 'Schema 必须是一个 JSON 对象'
  }
  if ((parsed as { type?: string }).type && (parsed as { type?: string }).type !== 'object') {
    return 'Schema 根节点的 type 必须是 "object"：抽取结果需要是一个字段集合'
  }
  return ''
}

async function handleSubmit() {
  const validationError = validate()
  if (validationError) {
    errorText.value = validationError
    return
  }

  submitting.value = true
  errorText.value = ''

  try {
    await templatesStore.createCustom({
      name: name.value.trim(),
      description: description.value.trim() || null,
      prompt_hint: promptHint.value.trim() || null,
      schema: parsedSchema.value as Record<string, unknown>,
    })
    message.success('模板已创建')
    open.value = false
    name.value = ''
    description.value = ''
    promptHint.value = ''
    schemaText.value = ''
  } catch (error) {
    // 后端的校验信息比前端更权威（它会做 JSON Schema 规范校验），直接把原文展示出来
    errorText.value = error instanceof ApiError ? error.message : '创建失败'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <a-modal
    v-model:open="open"
    title="新建自定义模板"
    :width="720"
    :confirm-loading="submitting"
    :ok-button-props="{ disabled: !canSubmit }"
    ok-text="创建"
    cancel-text="取消"
    @ok="handleSubmit"
  >
    <a-form layout="vertical">
      <a-form-item label="模板名称" required>
        <a-input v-model:value="name" placeholder="例如：采购单" :maxlength="128" />
      </a-form-item>

      <a-form-item label="用途说明">
        <a-input
          v-model:value="description"
          placeholder="这个模板用来抽取什么文档（可选）"
          :maxlength="500"
        />
      </a-form-item>

      <a-form-item
        label="JSON Schema"
        required
        :validate-status="errorText ? 'error' : undefined"
        :help="errorText || '标准 JSON Schema（Draft 2020-12），根节点必须是 object'"
      >
        <a-textarea
          v-model:value="schemaText"
          :rows="14"
          style="font-family: var(--sdp-mono); font-size: 12px"
          placeholder="粘贴 JSON Schema"
        />
      </a-form-item>

      <a-collapse ghost>
        <a-collapse-panel key="hint" header="业务规则（可选，会写进提示词）">
          <a-textarea
            v-model:value="promptHint"
            :rows="3"
            :maxlength="1000"
            placeholder="例如：金额只填数字；日期统一为 YYYY-MM-DD；无法确定的字段填 null，不要编造"
          />
          <div class="sdp-muted hint">
            这里写的内容会作为「业务规则」附加到提示词里，是提升抽取准确率最直接的手段。
          </div>
        </a-collapse-panel>
      </a-collapse>
    </a-form>
  </a-modal>
</template>

<style scoped>
.hint {
  margin-top: 8px;
  font-size: 12px;
}
</style>
