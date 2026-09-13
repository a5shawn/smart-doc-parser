import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { healthApi } from '@/api/client'
import { getApiKey, setApiKey } from '@/api/credentials'
import { ApiError } from '@/api/http'

export type ConnectionStatus = 'unknown' | 'checking' | 'ok' | 'failed'

export const useSettingsStore = defineStore('settings', () => {
  const apiKey = ref(getApiKey())
  const status = ref<ConnectionStatus>('unknown')
  const statusMessage = ref('')
  const serverVersion = ref('')
  const environment = ref('')

  const hasApiKey = computed(() => apiKey.value.trim().length > 0)

  function saveApiKey(value: string): void {
    const trimmed = value.trim()
    apiKey.value = trimmed
    setApiKey(trimmed)
    // 密钥变了，之前的连通性结论就作废了
    status.value = 'unknown'
    statusMessage.value = ''
  }

  /**
   * 探活。
   *
   * 打的是 `/health/ready` 而不是 `/health/live`：后者只证明进程活着、
   * 不检查数据库，用它做"连接测试"会给出"一切正常"的假象，
   * 而实际上任务一提交就失败。
   */
  async function testConnection(): Promise<boolean> {
    status.value = 'checking'
    statusMessage.value = ''

    try {
      const live = await healthApi.live()
      await healthApi.ready()

      serverVersion.value = live.version
      environment.value = live.environment
      status.value = 'ok'
      statusMessage.value = '连接正常，数据库可用'
      return true
    } catch (error) {
      status.value = 'failed'
      if (error instanceof ApiError) {
        statusMessage.value = error.isAuthError
          ? '接口密钥无效或未配置，请在下方填写'
          : error.message
      } else {
        statusMessage.value = '连接失败，请确认后端已启动'
      }
      return false
    }
  }

  return {
    apiKey,
    hasApiKey,
    status,
    statusMessage,
    serverVersion,
    environment,
    saveApiKey,
    testConnection,
  }
})
