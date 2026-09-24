<script setup lang="ts">
// 职责：数据溯源块 —— source_endpoint + fetched_at + trace 标签（限宽省略，完整值看 tooltip）
// 链路：ToolsView 工具调用结果区引入；溯源数据由后端内核联动层按实测填写，本组件只展示不加工
// 对齐：AGENTS.md §3 数据不出域（溯源可见性）+ §4 前端红线（纯展示组件，零请求零 mock）
import { computed } from 'vue'
import type { Provenance } from '../api'

const props = defineProps<{
  provenance: Provenance
  trace: string
}>()

// 属性读取收敛在 script（本 vue-tsc 版本对模板内嵌套对象属性推导不稳），模板只消费现成值
const sourceEndpoint = computed(() => props.provenance.source_endpoint ?? '')
const fetchedAt = computed(() => props.provenance.fetched_at ?? '')
// trace 串 32 位 hex 过长，展示截断值，完整值挂 tooltip
const traceBrief = computed(() =>
  props.trace && props.trace.length > 12 ? `${props.trace.slice(0, 12)}…` : props.trace,
)
</script>

<template>
  <div class="provenance">
    <el-tag v-if="sourceEndpoint" size="small" type="success" effect="plain">
      来源 {{ sourceEndpoint }}
    </el-tag>
    <span v-if="fetchedAt" class="muted">取数时刻 {{ fetchedAt }}</span>
    <el-tooltip v-if="trace" :content="trace" placement="top">
      <el-tag size="small" type="info" effect="plain">trace {{ traceBrief }}</el-tag>
    </el-tooltip>
  </div>
</template>

<style scoped>
.provenance {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin: 8px 0 0;
}
.provenance .muted {
  margin: 0;
  font-size: 12px;
}
</style>