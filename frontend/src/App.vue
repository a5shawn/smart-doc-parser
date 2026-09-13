<script setup lang="ts">
import { onBeforeUnmount, onMounted } from 'vue'
import zhCN from 'ant-design-vue/es/locale/zh_CN'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'

import AppLayout from '@/layouts/AppLayout.vue'
import { useTasksStore } from '@/stores/tasks'
import { useTemplatesStore } from '@/stores/templates'

dayjs.locale('zh-cn')

const tasksStore = useTasksStore()
const templatesStore = useTemplatesStore()

onMounted(() => {
  // 进度流在应用启动时就建立，全局只有一条。
  // 放在这里而不是某个页面里：切换页面时连接不该断开，
  // 否则后台跑着的任务进度会丢一段。
  tasksStore.connect()
  void templatesStore.load()
})

onBeforeUnmount(() => {
  tasksStore.disconnect()
})
</script>

<template>
  <!-- 中文语言包：日期选择器、分页器等组件的内置文案才会是中文 -->
  <a-config-provider :locale="zhCN">
    <AppLayout />
  </a-config-provider>
</template>
