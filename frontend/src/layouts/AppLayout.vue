<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useTasksStore } from '@/stores/tasks'

const route = useRoute()
const router = useRouter()
const tasksStore = useTasksStore()

const menuItems = [
  { key: 'extract', label: '抽取工作台', path: '/' },
  { key: 'history', label: '历史记录', path: '/history' },
  { key: 'settings', label: '设置', path: '/settings' },
]

// 详情页也要让"抽取工作台"保持高亮——它本质上是工作台的子视图
const selectedKeys = computed(() => {
  const name = route.name as string | undefined
  if (name === 'task-detail') return ['extract']
  return [name ?? 'extract']
})

/** 进度流的连接状态。断线时用户应当知道"看到的不是最新的"，而不是默默等。 */
const streamStatus = computed(() => {
  switch (tasksStore.connectionState) {
    case 'open':
      return { color: 'success' as const, text: '实时连接正常' }
    case 'connecting':
      return { color: 'processing' as const, text: '正在连接…' }
    case 'reconnecting':
      return { color: 'warning' as const, text: '连接中断，重连中…' }
    default:
      return { color: 'default' as const, text: '未连接' }
  }
})

/**
 * 菜单点击。
 *
 * 参数类型放宽到 `string | number`：ant-design-vue 的 `MenuInfo.key` 是联合类型，
 * 只声明 `string` 会与 `MenuClickEventHandler` 不兼容——这类"看着能跑但类型不过"的
 * 问题，正是靠 vue-tsc 在编译期拦下来的。
 */
function onMenuClick({ key }: { key: string | number }) {
  const item = menuItems.find((entry) => entry.key === String(key))
  if (item && route.path !== item.path) {
    void router.push(item.path)
  }
}
</script>

<template>
  <a-layout style="min-height: 100vh">
    <a-layout-sider theme="light" :width="216" style="border-right: 1px solid #f0f0f0">
      <div class="brand">
        <div class="brand__mark">文</div>
        <div>
          <div class="brand__title">智能文档解析</div>
          <div class="brand__sub">Document Parser</div>
        </div>
      </div>

      <a-menu
        mode="inline"
        :selected-keys="selectedKeys"
        :items="menuItems"
        style="border-inline-end: none"
        @click="onMenuClick"
      />

      <div class="sider-footer">
        <a-badge :status="streamStatus.color" :text="streamStatus.text" />
      </div>
    </a-layout-sider>

    <a-layout>
      <a-layout-content>
        <router-view />
      </a-layout-content>
    </a-layout>
  </a-layout>
</template>

<style scoped>
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 18px 20px 14px;
}

.brand__mark {
  width: 34px;
  height: 34px;
  border-radius: 9px;
  background: linear-gradient(135deg, #1677ff, #4096ff);
  color: #fff;
  font-size: 17px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.brand__title {
  font-size: 14px;
  font-weight: 600;
  line-height: 1.3;
}

.brand__sub {
  font-size: 11px;
  color: rgba(0, 0, 0, 0.35);
  letter-spacing: 0.04em;
}

.sider-footer {
  position: absolute;
  bottom: 16px;
  left: 20px;
  font-size: 12px;
}
</style>
