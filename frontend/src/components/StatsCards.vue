<script setup lang="ts">
import { computed } from 'vue'

import type { StatsOut } from '@/api/client'
import { formatCost, formatDuration, formatNumber } from '@/utils/format'

const props = defineProps<{ stats: StatsOut | null; loading?: boolean }>()

const successRatePercent = computed(() =>
  props.stats ? Math.round(props.stats.success_rate * 1000) / 10 : 0,
)

/**
 * 缓存命中率。
 *
 * 这个数字衡量的是 prompt 组织策略的效果——同样一份模板反复抽取时，
 * 前缀部分会命中 DeepSeek 的上下文缓存，单价只有未命中的几十分之一。
 * 把它显示出来，才能回答"这次优化到底省没省钱"。
 */
const cacheHitRate = computed(() => {
  const stats = props.stats
  if (!stats || stats.total_prompt_tokens === 0) return 0
  return Math.round((stats.total_cached_tokens / stats.total_prompt_tokens) * 1000) / 10
})
</script>

<template>
  <a-row :gutter="[16, 16]">
    <a-col :xs="12" :sm="12" :md="6">
      <a-card :bordered="false" :loading="loading">
        <a-statistic
          title="累计成本"
          :value="formatCost(stats?.total_cost_usd)"
          :value-style="{ fontSize: '22px' }"
        />
        <div class="sdp-muted stats-hint">
          单任务均价 {{ formatCost(stats?.avg_cost_per_task_usd) }}
        </div>
      </a-card>
    </a-col>

    <a-col :xs="12" :sm="12" :md="6">
      <a-card :bordered="false" :loading="loading">
        <a-statistic
          title="任务成功率"
          :value="successRatePercent"
          suffix="%"
          :value-style="{ fontSize: '22px' }"
        />
        <div class="sdp-muted stats-hint">
          成功 {{ stats?.completed_tasks ?? 0 }} / 失败 {{ stats?.failed_tasks ?? 0 }}
        </div>
      </a-card>
    </a-col>

    <a-col :xs="12" :sm="12" :md="6">
      <a-card :bordered="false" :loading="loading">
        <a-statistic
          title="token 总用量"
          :value="
            formatNumber((stats?.total_prompt_tokens ?? 0) + (stats?.total_completion_tokens ?? 0))
          "
          :value-style="{ fontSize: '22px' }"
        />
        <div class="sdp-muted stats-hint">缓存命中率 {{ cacheHitRate }}%</div>
      </a-card>
    </a-col>

    <a-col :xs="12" :sm="12" :md="6">
      <a-card :bordered="false" :loading="loading">
        <a-statistic
          title="平均耗时"
          :value="formatDuration(stats?.avg_latency_ms ?? null)"
          :value-style="{ fontSize: '22px' }"
        />
        <div class="sdp-muted stats-hint">
          文档 {{ stats?.total_documents ?? 0 }} 份 · 任务 {{ stats?.total_tasks ?? 0 }} 个
        </div>
      </a-card>
    </a-col>
  </a-row>
</template>

<style scoped>
.stats-hint {
  margin-top: 4px;
  font-size: 12px;
}
</style>
