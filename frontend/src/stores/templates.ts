import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { templatesApi, type CustomTemplateCreate, type TemplateOut } from '@/api/client'

export const useTemplatesStore = defineStore('templates', () => {
  const templates = ref<TemplateOut[]>([])
  const loading = ref(false)
  const loaded = ref(false)
  const error = ref('')

  const builtinTemplates = computed(() => templates.value.filter((item) => item.builtin))
  const customTemplates = computed(() => templates.value.filter((item) => !item.builtin))

  /** 按 key 取模板。用于把任务的 template_key 还原成中文名与字段标签。 */
  function findByKey(key: string): TemplateOut | undefined {
    return templates.value.find((item) => item.key === key)
  }

  async function load(force = false): Promise<void> {
    // 模板在会话期间基本不变，缓存住避免每次进页面都请求
    if (loaded.value && !force) return

    loading.value = true
    error.value = ''
    try {
      templates.value = await templatesApi.list()
      loaded.value = true
    } catch (err) {
      error.value = err instanceof Error ? err.message : '模板加载失败'
    } finally {
      loading.value = false
    }
  }

  async function createCustom(payload: CustomTemplateCreate): Promise<void> {
    await templatesApi.create(payload)
    await load(true)
  }

  async function removeCustom(templateId: string): Promise<void> {
    await templatesApi.remove(templateId)
    await load(true)
  }

  return {
    templates,
    builtinTemplates,
    customTemplates,
    loading,
    loaded,
    error,
    load,
    findByKey,
    createCustom,
    removeCustom,
  }
})
