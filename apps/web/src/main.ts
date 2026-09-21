// 职责：应用入口（挂载根组件、接线路由与 401 中央处理）
// 链路：index.html → main.ts → router（守卫）→ App.vue 壳 → 各视图；
//      401：api.ts request() 清 token 后回调本处 → 跳 /login（带 expired 标记，登录页复显失效提示）
// 对齐：AGENTS.md §4 前端红线（401 走中央处理，页面不自跳）
import { createApp } from 'vue'
import App from './App.vue'
import { setUnauthorizedHandler } from './api'
import router from './router'
import './style.css'

// 401 中央处理：会话失效统一回调到这里，路由跳转只在中央做一次
setUnauthorizedHandler(() => {
  router.push({ path: '/login', query: { expired: '1' } })
})

createApp(App).use(router).mount('#app')
