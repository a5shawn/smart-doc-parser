<script setup lang="ts">
import { WarningOutlined } from '@ant-design/icons-vue'
import { computed } from 'vue'

import type { ExtractionResultOut, TemplateFieldOut, TemplateOut } from '@/api/client'
import { formatFieldValue, isObjectArray } from '@/utils/format'

const props = defineProps<{
  result: ExtractionResultOut
  template?: TemplateOut
  taskId: string
}>()

/**
 * 字段定义来自模板。
 *
 * 这正是 schema 驱动的价值：中文标签、字段顺序、哪些是数组，
 * 全部由模板这一处定义决定——前端不需要为每种模板写一套渲染逻辑，
 * 新增模板时前端代码零改动。
 */
const fields = computed<TemplateFieldOut[]>(() => props.template?.fields ?? [])

const scalarFields = computed(() => fields.value.filter((field) => field.type !== 'array'))
const arrayFields = computed(() => fields.value.filter((field) => field.type === 'array'))

const data = computed<Record<string, unknown>>(() => props.result.data ?? {})

/** 模型自创的字段（不在模板里）。展示出来但不参与字段卡片排布。 */
const extraKeys = computed(() => {
  const known = new Set(fields.value.map((field) => field.name))
  return Object.keys(data.value).filter((key) => !known.has(key))
})

const warnings = computed(() => props.result.warnings ?? [])

/** 按问题类型分组。同一类问题在界面上聚一次就够，不必逐条铺开。 */
const warningsByKind = computed(() => {
  const groups = new Map<string, { label: string; items: typeof warnings.value }>()
  const LABELS: Record<string, string> = {
    input_truncated: '文档被截断',
    missing_required: '必填字段未抽到',
    type_coerced: '类型已自动转换',
    nullish_string: '空值已归一化',
    unexpected_field: '模型多返回了字段',
    type_mismatch: '类型不符',
    enum_mismatch: '取值不在允许范围',
    wrapped_scalar_as_array: '单值已包装成数组',
  }

  for (const warning of warnings.value) {
    const key = warning.kind
    const group = groups.get(key) ?? { label: LABELS[key] ?? key, items: [] }
    group.items.push(warning)
    groups.set(key, group)
  }
  return [...groups.values()]
})

/** 值为空的字段排序到后面——用户最关心的是抽到了什么。 */
const orderedScalarFields = computed(() =>
  [...scalarFields.value].sort((a, b) => {
    const aEmpty = isEmpty(data.value[a.name])
    const bEmpty = isEmpty(data.value[b.name])
    if (aEmpty === bEmpty) return 0
    return aEmpty ? 1 : -1
  }),
)

function isEmpty(value: unknown): boolean {
  return value === null || value === undefined || value === ''
}

function cellText(value: unknown): string {
  return formatFieldValue(value)
}

/** 数组字段的列定义，来自模板里的 item_fields。 */
function columnsFor(field: TemplateFieldOut) {
  // 生成的类型里该字段是可选（后端定义带默认值），因此要兜底
  return (field.item_fields ?? []).map((child) => ({
    title: child.label,
    dataIndex: child.name,
    key: child.name,
    ellipsis: true,
  }))
}

function rowsFor(field: TemplateFieldOut): Record<string, unknown>[] {
  const value = data.value[field.name]
  return Array.isArray(value) ? (value as Record<string, unknown>[]) : []
}
</script>

<template>
  <div class="result-panel">
    <!-- 文档被截断是影响结果可信度的前提，必须放在最显眼的位置 -->
    <a-alert
      v-if="result.truncated"
      type="warning"
      show-icon
      class="result-panel__alert"
      message="文档过长，中间部分内容已被省略"
      description="省略处的字段可能因此未被抽到。如需完整抽取，可调大后端环境变量 MAX_INPUT_CHARS。"
    />

    <a-alert
      v-if="warnings.length > 0"
      type="info"
      show-icon
      class="result-panel__alert"
      :message="`抽取结果有 ${warnings.length} 处需要注意`"
    >
      <template #description>
        <div v-for="group in warningsByKind" :key="group.label" class="result-panel__warning-group">
          <strong>{{ group.label }}</strong>
          <ul>
            <li v-for="(item, index) in group.items" :key="index">
              <code>{{ item.path }}</code>
              <span v-if="item.message"> — {{ item.message }}</span>
            </li>
          </ul>
        </div>
      </template>
    </a-alert>

    <!-- 标量字段：描述列表 -->
    <a-descriptions
      v-if="orderedScalarFields.length > 0"
      bordered
      size="small"
      :column="{ xs: 1, sm: 1, md: 2, lg: 2, xl: 3 }"
      class="result-panel__descriptions"
    >
      <a-descriptions-item
        v-for="field in orderedScalarFields"
        :key="field.name"
        :label="field.label"
      >
        <span v-if="isEmpty(data[field.name])" class="sdp-muted">未抽取到</span>
        <span v-else class="sdp-field-value">{{ cellText(data[field.name]) }}</span>
      </a-descriptions-item>
    </a-descriptions>

    <!-- 数组字段：对象数组渲染成表格，字符串数组渲染成标签 -->
    <div v-for="field in arrayFields" :key="field.name" class="result-panel__array">
      <h4 class="result-panel__array-title">{{ field.label }}</h4>

      <template v-if="isObjectArray(data[field.name])">
        <a-table
          :columns="columnsFor(field)"
          :data-source="rowsFor(field)"
          :pagination="false"
          size="small"
          row-key="__index"
          :scroll="{ x: 'max-content' }"
        >
          <template #bodyCell="{ column, record }">
            <span class="sdp-field-value">{{ cellText(record[column.key as string]) }}</span>
          </template>
        </a-table>
      </template>

      <template v-else-if="Array.isArray(data[field.name])">
        <a-space :size="[6, 6]" wrap>
          <a-tag v-for="(item, index) in data[field.name] as unknown[]" :key="index">
            {{ cellText(item) }}
          </a-tag>
          <span v-if="(data[field.name] as unknown[]).length === 0" class="sdp-muted">
            未抽取到
          </span>
        </a-space>
      </template>

      <span v-else class="sdp-muted">未抽取到</span>
    </div>

    <!-- 模型自创的字段：展示但不鼓励，用户需要知道它们不在模板定义里 -->
    <div v-if="extraKeys.length > 0" class="result-panel__array">
      <h4 class="result-panel__array-title"><WarningOutlined /> 模板之外的字段</h4>
      <a-descriptions bordered size="small" :column="2">
        <a-descriptions-item v-for="key in extraKeys" :key="key" :label="key">
          <span class="sdp-field-value">{{ cellText(data[key]) }}</span>
        </a-descriptions-item>
      </a-descriptions>
    </div>

    <a-empty v-if="fields.length === 0 && extraKeys.length === 0" description="抽取结果为空" />
  </div>
</template>

<style scoped>
.result-panel__alert {
  margin-bottom: 14px;
}

.result-panel__warning-group + .result-panel__warning-group {
  margin-top: 8px;
}

.result-panel__warning-group ul {
  margin: 4px 0 0;
  padding-left: 18px;
}

.result-panel__descriptions :deep(.ant-descriptions-item-label) {
  width: 140px;
  font-weight: 500;
  background: #fafafa;
}

.result-panel__array {
  margin-top: 20px;
}

.result-panel__array-title {
  margin: 0 0 10px;
  font-size: 14px;
  font-weight: 600;
}
</style>
