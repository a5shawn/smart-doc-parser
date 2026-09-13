/// <reference types="vitest/config" />
import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { AntDesignVueResolver } from 'unplugin-vue-components/resolvers'
import { defineConfig, loadEnv } from 'vite'

const ROOT_DIR = fileURLToPath(new URL('..', import.meta.url))
const SRC_DIR = fileURLToPath(new URL('./src', import.meta.url))

//: 本地开发时后端跑在哪个地址（Docker 部署时由 nginx 反代，用不到这个）
const DEV_BACKEND = process.env.VITE_DEV_BACKEND ?? 'http://127.0.0.1:8000'

export default defineConfig(({ mode }) => {
  // envDir 指向仓库根目录：**前端与后端、docker-compose 共用同一份 .env**。
  // 这正是「本地和线上配置保持统一」的实现方式——只有一处配置来源。
  const env = loadEnv(mode, ROOT_DIR, 'VITE_')
  const apiBase = env.VITE_API_BASE_URL || '/api/v1'

  return {
    envDir: ROOT_DIR,
    plugins: [
      vue(),
      // Ant Design Vue 组件按需自动引入，模板里直接写 <a-upload> 即可
      Components({
        resolvers: [AntDesignVueResolver({ importStyle: false })],
        dts: 'src/components.d.ts',
      }),
    ],
    resolve: {
      alias: { '@': SRC_DIR },
    },
    server: {
      host: '0.0.0.0',
      port: 5173,
      proxy: {
        [apiBase]: {
          target: DEV_BACKEND,
          changeOrigin: true,
          // SSE 是长连接。**必须把超时关掉**，否则 dev server 会在默认超时后
          // 掐断连接，表现为进度条每隔一段时间就重连一次、状态跳变。
          timeout: 0,
          proxyTimeout: 0,
        },
      },
    },
    build: {
      outDir: 'dist',
      sourcemap: false,
      chunkSizeWarningLimit: 1200,
    },
    test: {
      environment: 'node',
      include: ['src/**/*.spec.ts'],
    },
  }
})
