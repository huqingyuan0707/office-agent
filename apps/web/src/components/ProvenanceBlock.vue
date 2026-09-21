<script setup lang="ts">
// 职责：数据溯源块 —— source_endpoint + fetched_at + trace 徽标（限宽省略，完整值看 title）
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
</script>

<template>
  <div class="provenance">
    <span>数据出处：</span>
    <code v-if="sourceEndpoint">{{ sourceEndpoint }}</code>
    <span v-if="fetchedAt" class="muted">（取数时刻 {{ fetchedAt }}）</span>
    <span v-if="trace" class="badge trace" :title="trace">trace {{ trace }}</span>
  </div>
</template>
