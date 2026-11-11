import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { Button, Tag } from 'vant'
import 'vant/es/button/style/index'
import 'vant/es/tag/style/index'

import App from './App.vue'
import { router } from './router'
import './styles.css'

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(Button)
app.use(Tag)
app.mount('#app')

if (import.meta.env.PROD && 'serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {
      // SW 注册失败不影响主流程
    })
  })
}
