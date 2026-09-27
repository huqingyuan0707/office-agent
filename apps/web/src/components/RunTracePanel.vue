<script setup lang="ts">
// 职责：执行链路调试面板（纯展示组件）—— 管理员查看单条 Agent 回复的完整执行过程
// 链路：ChatView 传 run.trace（GET /runs/{id} 仅管理员透出）+ 会话 ID → 按执行时序渲染
//      意图/思考/路由/工具调用/工具返回/汇总分段（思考蓝/路由绿/调用橙/返回灰）；默认收起，
//      右上收起 + 导出JSON（trace 原样落盘供复现）；零请求零 mock
// 对齐：AGENTS.md §4 前端红线（纯展示组件零请求；样式 var(--*) token + scoped；箭头函数）
import type { RunTrace } from '../api'

const props = defineProps<{ trace: RunTrace; sessionId: string }>()

// 受控显隐：挂载即展开，右上「收起」抛回父级关闭（显隐状态归消息气泡持有，刷新不丢）
const emit = defineEmits(['close'])

const collapse = () => {
  emit('close')
}

// 步骤类型 → 左侧色条语义（思考蓝 / 路由绿 / 工具调用橙 / 工具返回灰，未知类型一律灰）
const KIND_TONE: { [key: string]: string } = {
  intent: '#409eff',
  thought: '#409eff',
  route: '#67c23a',
  tool_call: '#e6a23c',
  tool_result: '#909399',
  summary: '#f56c6c',
}

const toneOf = (stepType: string) => KIND_TONE[stepType] ?? '#8c959f'

const costText = (ms: unknown) => (typeof ms === 'number' ? `${ms}ms` : '')

// 导出调试日志：trace 原样下载 JSON（评测与问题复现用；与下载 Word 同一 Blob 链路）
const exportJson = () => {
  const text = JSON.stringify({ session_id: props.sessionId, trace: props.trace }, null, 2)
  const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }))
  const a = document.createElement('a')
  a.href = url
  a.download = `trace-${props.trace.run_id || 'run'}.json`
  a.click()
  URL.revokeObjectURL(url)
}
</script>

<template>
  <div class="trace-panel">
    <div class="trace-head">
      <span class="trace-title">【执行链路｜会话ID: {{ sessionId || '—' }}】</span>
      <div class="trace-actions">
        <el-button size="small" text type="primary" @click="exportJson">导出JSON</el-button>
        <el-button size="small" text type="primary" @click="collapse">收起</el-button>
      </div>
    </div>
    <div class="trace-body">
      <div v-for="s in props.trace.steps" :key="`${s.step_no}-${s.step_type}`" class="trace-step">
        <span class="step-bar" :style="{ background: toneOf(s.step_type) }" />
        <div class="step-main">
          <div class="step-line">
            <strong>{{ s.step_label || s.step_type }}</strong>
            <el-tag v-if="costText(s.time_cost_ms)" size="small" effect="plain" round>
              {{ costText(s.time_cost_ms) }}
            </el-tag>
            <el-tag v-if="s.truncated" size="small" type="warning" effect="light" round>
              已截断
            </el-tag>
          </div>
          <pre class="step-content">{{ s.content }}</pre>
        </div>
      </div>
      <div class="trace-foot muted">
        本次总耗时：{{ props.trace.total_ms ?? '—' }}ms ｜ 智能体：{{ props.trace.agent }} ｜
        规划来源：{{ props.trace.planner_source || '—' }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.trace-panel {
  margin-top: 10px;
  background: #f6f8fa;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
}
.trace-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 12px;
}
.trace-title {
  font-size: 12px;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.trace-actions {
  display: flex;
  gap: 4px;
  flex: none;
}
.trace-body {
  padding: 4px 12px 10px;
  border-top: 1px dashed var(--line);
}
.trace-step {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}
.step-bar {
  flex: none;
  width: 3px;
  border-radius: 2px;
}
.step-main {
  flex: 1;
  min-width: 0;
}
.step-line {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}
.step-content {
  margin: 4px 0 0;
  padding: 6px 8px;
  background: #fff;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
}
.trace-foot {
  margin-top: 8px;
  font-size: 12px;
}
</style>
