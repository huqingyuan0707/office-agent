<script setup lang="ts">
// 职责：状态徽标（纯展示组件）—— approved 绿 / rejected 红 / pending 黄 / 其余灰，文案优先取 label
// 链路：Tasks / Approvals / Agents 等视图的状态列引入使用，数据由父级传入，组件零请求
// 对齐：AGENTS.md §4 前端红线（样式 var(--*) token，颜色语义与全局徽标类一致）
import { computed } from 'vue'

const props = defineProps<{ status: string; label?: string }>()

const tone = computed(() => ({
  ok: props.status === 'approved',
  danger: props.status === 'rejected',
  warn: props.status === 'pending',
}))
</script>

<template>
  <span class="badge" :class="tone">{{ label || status }}</span>
</template>
