/// <reference types="vite/client" />

/**
 * 环境变量的类型声明。
 *
 * 只有 VITE_ 前缀的变量会被 Vite 注入前端产物——这一点是安全设计：
 * 仓库根目录的 .env 里同时放着 DeepSeek 密钥，而它没有 VITE_ 前缀，
 * 因此**永远不会**被打进浏览器代码里。
 */
interface ImportMetaEnv {
  /** 后端接口基地址。默认相对路径 /api/v1，Docker 与本地开发行为一致。 */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

declare module '*.vue' {
  import type { DefineComponent } from 'vue'

  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}
