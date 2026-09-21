// 职责：Vite 构建配置（极简：仅 Vue 插件；构建产物默认输出 dist）
// 链路：dev/build 均由本配置驱动；前端直连后端 base 地址，不设 dev 代理
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
})
