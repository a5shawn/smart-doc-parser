import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

/**
 * 用 history 模式（URL 里没有 `#`），因此**部署时必须让 nginx 做 SPA fallback**：
 * 直接访问 `/tasks/xxx` 时服务器要返回 `index.html` 而不是 404。
 * 配置见 frontend/nginx/nginx.conf 的 `try_files`。
 */
const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'extract',
    component: () => import('@/views/ExtractView.vue'),
    meta: { title: '抽取工作台' },
  },
  {
    path: '/tasks/:id',
    name: 'task-detail',
    component: () => import('@/views/TaskDetailView.vue'),
    props: true,
    meta: { title: '任务详情' },
  },
  {
    path: '/history',
    name: 'history',
    component: () => import('@/views/HistoryView.vue'),
    meta: { title: '历史记录' },
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('@/views/SettingsView.vue'),
    meta: { title: '设置' },
  },
  {
    // 兜底：未知路径回首页，而不是留一个空白页
    path: '/:pathMatch(.*)*',
    redirect: '/',
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

router.afterEach((to) => {
  const title = to.meta.title as string | undefined
  document.title = title ? `${title} · 智能文档解析服务` : '智能文档解析服务'
})
