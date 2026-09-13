<script setup lang="ts">
import { CheckCircleOutlined, CloseCircleOutlined } from '@ant-design/icons-vue'
import { message } from 'ant-design-vue'
import { computed, onMounted, ref } from 'vue'

import { API_BASE_URL } from '@/api/http'
import { templatesApi } from '@/api/client'
import { useSettingsStore } from '@/stores/settings'
import { useTemplatesStore } from '@/stores/templates'

const settingsStore = useSettingsStore()
const templatesStore = useTemplatesStore()

const draftKey = ref(settingsStore.apiKey)
const testing = ref(false)
const templateCount = ref<number | null>(null)

const statusMeta = computed(() => {
  switch (settingsStore.status) {
    case 'ok':
      return {
        type: 'success' as const,
        icon: CheckCircleOutlined,
        text: settingsStore.statusMessage,
      }
    case 'failed':
      return {
        type: 'error' as const,
        icon: CloseCircleOutlined,
        text: settingsStore.statusMessage,
      }
    case 'checking':
      return { type: 'info' as const, icon: undefined, text: '正在检测…' }
    default:
      return { type: 'info' as const, icon: undefined, text: '尚未检测连接' }
  }
})

function save() {
  settingsStore.saveApiKey(draftKey.value)
  message.success('已保存到浏览器本地')
}

async function testConnection() {
  // 先把输入框里的值存下来再测，避免"填了但没保存"造成的困惑
  settingsStore.saveApiKey(draftKey.value)
  testing.value = true
  try {
    const ok = await settingsStore.testConnection()
    if (ok) {
      const templates = await templatesApi.list()
      templateCount.value = templates.length
      await templatesStore.load(true)
    } else {
      templateCount.value = null
    }
  } finally {
    testing.value = false
  }
}

onMounted(() => {
  if (settingsStore.status === 'unknown') {
    void testConnection()
  }
})
</script>

<template>
  <div class="sdp-page">
    <header class="sdp-page__header">
      <h1 class="sdp-page__title">设置</h1>
      <p class="sdp-page__subtitle">接口连接与访问密钥。</p>
    </header>

    <a-card :bordered="false" title="接口连接">
      <a-descriptions :column="1" size="small" bordered>
        <a-descriptions-item label="接口地址">
          <a-typography-text code>{{ API_BASE_URL }}</a-typography-text>
          <div class="sdp-muted settings__hint">
            相对路径。Docker 部署时由 nginx 反代，本地开发时由 Vite 代理，
            两种形态下前端代码完全一致。
          </div>
        </a-descriptions-item>

        <a-descriptions-item label="服务版本">
          <span v-if="settingsStore.serverVersion">
            {{ settingsStore.serverVersion }}
            <a-tag>{{ settingsStore.environment }}</a-tag>
          </span>
          <span v-else class="sdp-muted">—</span>
        </a-descriptions-item>

        <a-descriptions-item label="可用模板">
          <span v-if="templateCount !== null">{{ templateCount }} 个</span>
          <span v-else class="sdp-muted">—</span>
        </a-descriptions-item>
      </a-descriptions>

      <a-alert
        class="settings__status"
        :type="statusMeta.type"
        show-icon
        :message="statusMeta.text"
      />

      <a-space :size="12" class="settings__actions">
        <a-button type="primary" :loading="testing" @click="testConnection"> 测试连接 </a-button>
      </a-space>
    </a-card>

    <a-card :bordered="false" title="访问密钥" class="settings__card">
      <p class="sdp-muted">
        仅当后端 <code>AUTH_ENABLED=true</code> 时需要填写。 后端默认关闭鉴权，此时留空即可。
      </p>

      <a-space :size="12" class="settings__actions">
        <a-input-password
          v-model:value="draftKey"
          placeholder="X-API-Key"
          style="width: 340px"
          @press-enter="save"
        />
        <a-button @click="save">保存</a-button>
      </a-space>

      <a-alert type="warning" show-icon class="settings__warning">
        <template #message>关于密钥存放位置的说明</template>
        <template #description>
          <p>
            密钥保存在浏览器的 <code>localStorage</code> 里，因此
            <strong>任何 XSS 都能把它读走</strong>。这是演示场景下的取舍， 生产环境应当改用后端会话
            + HttpOnly Cookie，或把密钥留在服务端代理层， 不下发到浏览器。
          </p>
          <p style="margin-bottom: 0">
            另外注意：浏览器里输入的任何内容都无法隐藏于用户本人，
            因此这个方案只适用于「自己给自己配一个 key」的场景。
          </p>
        </template>
      </a-alert>
    </a-card>
  </div>
</template>

<style scoped>
.settings__card {
  margin-top: 16px;
}

.settings__status {
  margin-top: 16px;
}

.settings__actions {
  margin-top: 12px;
}

.settings__hint {
  font-size: 12px;
  margin-top: 4px;
}

.settings__warning {
  margin-top: 16px;
}
</style>
