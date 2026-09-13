import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import { router } from './router'
import './styles/main.css'

const app = createApp(App)

app.use(createPinia())
app.use(router)

// 全局错误兜底：组件里漏掉的异常至少能在控制台看到完整堆栈，
// 而不是变成一个白屏让用户和你都摸不着头脑
app.config.errorHandler = (error, _instance, info) => {
  console.error('[vue] 未捕获的组件错误', info, error)
}

app.mount('#app')
