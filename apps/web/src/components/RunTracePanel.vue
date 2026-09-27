<script setup lang="ts">
// 职责：执行链路调试面板（纯展示组件）—— 管理员查看单条 Agent 回复的完整执行过程
// 链路：ChatView 传 run.trace（GET /runs/{id} 仅管理员透出）+ 会话 ID → 按执行时序渲染
//      意图/思考/路由/工具调用/工具返回/汇总分段；默认收起，右上角收起/展开；零请求零 mock
// 对齐：AGENTS.md §4 前端红线（纯展示组件零请求；样式 var(--*) token + scoped；箭头函数）
import type { RunTrace } from '../api'

const props = defineProps<{ trace: RunTrace; sessionId: string }>()

// 受控显隐：挂载即展开，右上「收起」抛回父级关闭（显隐状态归消息气泡持有，刷新不丢）
const emit = defineEmits(['close'])

const collapse = () => {
  emit('close')
}

// 步骤类型 → 左侧色条语义（未知类型一律灰，不猜不编造）
const KIND_TONE: { [key: string]: string } = {
  intent: '#1f6feb',
  thought: '#8250df',
  route: '#1a7f37',
  tool_call: '#9a6700',
  tool_result: '#0969da',
  summary: '#cf222e',
}

const toneOf = (stepType: string) => KIND_TONE[stepType] ?? '#8c959f'

const costText = (ms: unknown) => (typeof ms === 'number' ? `${ms}ms` : '')
</script>

<template>
  <div class="trace-panel">
    <div class="trace-head">
      <span class="trace-title">【执行链路｜会话ID: {{ sessionId || '—' }}】</span>
      <el-button size="small" text type="primary" @click="collapse">收起</el-button>
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
