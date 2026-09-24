// 职责：Vite 构建配置（Vue 插件 + Element Plus 按需引入；构建产物默认输出 dist）
// 链路：dev/build 均由本配置驱动；前端直连后端 base 地址，不设 dev 代理；
//      EP 组件经 unplugin 解析器按用到的逐个注入（含样式），禁止全局全量引入（frontend-code-style §5）；
//      dts 落 src/types/（tsconfig include 范围内，vue-tsc 构建门禁才认得 el-* 全局组件）
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

export default defineConfig({
  plugins: [
    vue(),
    AutoImport({ resolvers: [ElementPlusResolver()], dts: 'src/types/auto-imports.d.ts' }),
    Components({ resolvers: [ElementPlusResolver()], dts: 'src/types/components.d.ts' }),
  ],
})
