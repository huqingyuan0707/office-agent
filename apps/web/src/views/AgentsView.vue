<script setup lang="ts">
// 职责：智能体页 —— agent 卡片（描述/工具白名单/max_steps）→ 选定 agent + 输目标 → 发起 run →
//       2s 轮询 run 详情至终态 → 步骤时间线（step_index/tool/status/args 摘要/trace_id 徽标）；
//       run 挂起或步骤 pending → 中文提示引导去审批页
// 链路：router /agents → api.listAgents / api.createRun / api.getRun；
//       onUnmounted 清轮询定时器防泄漏；列表失败 → 壳红条，发起/轮询失败 → 本页红条
// 对齐：AGENTS.md §4 前端红线（真实接口零 mock、禁直写 fetch、箭头函数）
import { inject, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { createRun, getRun, listAgents } from '../api'
import type { AgentItem, RunItem } from '../api'
import StatusBadge from '../components/StatusBadge.vue'

const shell = inject('shellError') as {
  showError: (msg: string) => unknown
  clearError: () => unknown
}

// ---------- agent 列表与发起表单 ----------
const agents = ref<AgentItem[]>([])
const agentsLoading = ref(true)
const currentAgent = ref('') // 选中的 agent id（卡片高亮）
const goal = ref('')
const starting = ref(false)

// ---------- 运行与轮询 ----------
const run = ref<RunItem | null>(null)
const runError = ref('') // 发起 / 轮询失败红条（本页级，与壳红条分开）
const polling = ref(false)
let timer: ReturnType<typeof setInterval> | undefined

// 终态集合：轮询到即停（终态命名以后端 run 契约为准，命中任一即收敛）
const TERMINAL_STATUSES = ['succeeded', 'failed', 'cancelled', 'completed']

const stopPolling = () => {
  if (timer !== undefined) {
    clearInterval(timer)
    timer = undefined
  }
  polling.value = false
}

const refreshRun = async () => {
  if (!run.value) return
  try {
    run.value = await getRun(run.value.run_id)
    if (TERMINAL_STATUSES.includes(run.value.status)) stopPolling()
  } catch (e) {
    runError.value = (e as Error).message || '运行状态刷新失败'
    stopPolling()
  }
}

const startRun = async () => {
  if (!currentAgent.value || !goal.value.trim() || starting.value) return
  runError.value = ''
  shell.clearError()
  starting.value = true
  try {
    run.value = await createRun(currentAgent.value, goal.value.trim())
    stopPolling()
    polling.value = true
    timer = setInterval(refreshRun, 2000) // 2s 轮询，终态即停
  } catch (e) {
    runError.value = (e as Error).message || '发起运行失败'
  } finally {
    starting.value = false
  }
}

// 挂起判定：run 状态表明挂起，或任一步骤仍 pending → 引导去审批页处理
const suspended = () => {
  const r = run.value
  if (!r) return false
  return (
    ['pending', 'awaiting_approval', 'suspended'].includes(r.status) ||
    (r.steps ?? []).some((s) => s.status === 'pending')
  )
}

// args 摘要：超长截断，完整值在 title 里
const argsBrief = (args: unknown) => {
  const text = JSON.stringify(args ?? {})
  return text.length > 60 ? `${text.slice(0, 60)}…` : text
}

const loadAgents = async () => {
  agentsLoading.value = true
  shell.clearError()
  try {
    agents.value = (await listAgents()).items
  } catch (e) {
    agents.value = []
    shell.showError((e as Error).message || '智能体列表加载失败')
  } finally {
    agentsLoading.value = false
  }
}

const pickAgent = (id: string) => {
  currentAgent.value = id
}

onMounted(loadAgents)
onUnmounted(stopPolling) // 卸载清定时器，防离开页面后仍轮询
</script>

<template>
  <div class="grid">
    <!-- 左区：agent 卡片 -->
    <section class="card">
      <h2>
        智能体
        <span v-if="agents.length" class="badge">{{ agents.length }}</span>
      </h2>
      <p v-if="agentsLoading" class="empty">加载中…</p>
      <div v-else-if="!agents.length" class="empty">暂无可用智能体</div>
      <template v-else>
        <div
          v-for="a in agents"
          :key="a.name"
          :class="['tool-card', { active: a.name === currentAgent }]"
          @click="pickAgent(a.name)"
        >
          <div class="tool-head">
            <strong>{{ a.name }}</strong>
            <span v-if="a.max_steps !== undefined" class="badge">max_steps {{ a.max_steps }}</span>
          </div>
          <p class="muted">{{ a.description }}</p>
          <div v-if="a.tools?.length" class="tool-head agent-tools">
            <span v-for="t in a.tools" :key="t" class="badge">{{ t }}</span>
          </div>
        </div>
      </template>
    </section>

    <!-- 右区：发起运行 -->
    <section class="card">
      <h2>发起运行</h2>
      <label class="field">
        <span>智能体</span>
        <input :value="currentAgent || ''" placeholder="点击左侧智能体卡片选择" readonly />
      </label>
      <label class="field">
        <span>目标</span>
        <textarea v-model="goal" rows="4" placeholder="用一句话描述这次要让智能体完成什么"></textarea>
      </label>
      <button
        class="btn primary"
        :disabled="starting || !currentAgent || !goal.trim()"
        @click="startRun"
      >
        {{ starting ? '发起中…' : '启动运行' }}
      </button>

      <div v-if="runError" class="result fail">{{ runError }}</div>
    </section>
  </div>

  <!-- 运行详情：步骤时间线 -->
  <section v-if="run" class="card block-gap">
    <h2>
      运行详情
      <span class="badge">{{ run.status }}</span>
      <span v-if="polling" class="badge warn">轮询中</span>
    </h2>
    <p class="muted">运行 ID：<span class="mono">{{ run.run_id }}</span></p>
    <div v-if="suspended()" class="err-bar">
      <span class="err-text">存在待审批步骤，请到审批页处理</span>
      <RouterLink class="err-link" to="/approvals">前往审批页 →</RouterLink>
    </div>
    <div v-if="run.steps?.length" class="timeline">
      <div v-for="s in run.steps" :key="s.step_index" class="timeline-item">
        <div class="tool-head">
          <span class="badge">步骤 {{ s.step_index }}</span>
          <strong class="mono">{{ s.tool }}</strong>
          <StatusBadge :status="s.status" />
          <span v-if="s.trace_id" class="badge trace mono" :title="s.trace_id">
            trace {{ s.trace_id }}
          </span>
        </div>
        <p v-if="s.args !== undefined" class="muted mono args-brief" :title="JSON.stringify(s.args)">
          args {{ argsBrief(s.args) }}
        </p>
      </div>
    </div>
    <p v-else class="empty">暂无步骤记录</p>
  </section>
</template>
