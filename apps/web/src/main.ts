// 职责：应用入口，挂载根组件并引入全局样式
// 链路：index.html → main.ts → App.vue（登录卡 / 主面板）
import { createApp } from 'vue'
import App from './App.vue'
import './style.css'

createApp(App).mount('#app')
