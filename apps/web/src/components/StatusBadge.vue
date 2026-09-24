<script setup lang="ts">
// 职责：状态徽标（纯展示组件）—— 以 el-tag 承载状态语义色（成功绿 / 驳回红 / 待处理黄 / 其余灰）
// 链路：Tasks / Approvals / Agents / Workflows / Chat 等视图的状态列引入使用，数据由父级传入，组件零请求
// 对齐：AGENTS.md §4 前端红线（纯展示组件零请求零 mock；颜色语义由全局 EP 主题 token 派生）
import { computed } from 'vue'

const props = defineProps<{ status: string; label?: string }>()

// 状态 → el-tag 语义色（未知状态一律灰，不猜不编造）
const TONE: { [key: string]: string } = {
  approved: 'success',
  succeeded: 'success',
  completed: 'success',
  done: 'success',
  rejected: 'danger',
  failed: 'danger',
  cancelled: 'danger',
  error: 'danger',
  pending: 'warning',
  pending_approval: 'warning',
  awaiting_approval: 'warning',
  suspended: 'warning',
  running: 'primary',
  doing: 'primary',
  skipped: 'info',
}

const tone = computed(() => TONE[props.status] ?? 'info')
</script>

<template>
  <el-tag :type="tone" size="small" effect="light" round>{{ label || status }}</el-tag>
</template>