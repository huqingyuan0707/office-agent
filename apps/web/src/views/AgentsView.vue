<script setup lang="ts">
// 职责：智能体页（Element Plus 版）—— agent 卡片（描述/工具白名单/max_steps）→ 选定 agent +
//       输目标 → 发起 run → 2s 轮询 run 详情至终态 → 步骤时间线（step_index/tool/status/args
//       摘要/trace 徽标）；run 挂起或步骤 pending → 提示引导去审批页
// 链路：router /agents → api.listAgents / api.createRun / api.getRun；
//       onUnmounted 清轮询定时器防泄漏；列表失败 → 壳红条，发起/轮询失败 → 本页红条
// 对齐：AGENTS.md §4 前端红线（真实接口零 mock、禁直写 fetch、箭头函数、var(--*) token + scoped）
import { inject, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { Check, MagicStick, VideoPlay } from '@element-plus/icons-vue'
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
const currentAgent = ref('') // 选中的 agent 名（卡片高亮）
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

// 时间线节点类型：失败红、挂起/运行黄、成功绿、其余蓝
const stepType = (status: string) => {
  if (['failed', 'cancelled', 'error'].includes(status)) return 'danger'
  if (['pending', 'running'].includes(status)) return 'warning'
  if (['succeeded', 'completed', 'done'].includes(status)) return 'success'
  return 'primary'
}

// args 摘要：超长截断，完整值在 tooltip 里
const argsBrief = (args: unknown) => {
  const text = JSON.stringify(args ?? {})
  return text.length > 70 ? `${text.slice(0, 70)}…` : text
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

const pickAgent = (name: string) => {
  currentAgent.value = name
}

onMounted(loadAgents)
onUnmounted(stopPolling) // 卸载清定时器，防离开页面后仍轮询
</script>

<template>
  <div class="grid">
    <!-- 左区：agent 卡片（点选高亮） -->
    <el-card shadow="never">
      <template #header>
        <div class="page-head">
          <span class="card-title">智能体</span>
          <el-tag v-if="agents.length" size="small" type="info" round>{{ agents.length }} 个</el-tag>
          <span class="muted head-hint">选择左侧卡片后填写目标</span>
        </div>
      </template>

      <el-skeleton v-if="agentsLoading" :rows="4" animated />
      <el-empty v-else-if="!agents.length" :image-size="80" description="暂无可用智能体" />
      <div v-else class="agent-list">
        <el-card
          v-for="a in agents"
          :key="a.name"
          :class="['agent-item', { active: a.name === currentAgent }]"
          shadow="hover"
          @click="pickAgent(a.name)"
        >
          <div class="agent-top">
            <el-icon class="agent-icon"><MagicStick /></el-icon>
            <strong class="agent-name">{{ a.name }}</strong>
            <el-icon v-if="a.name === currentAgent" class="agent-check"><Check /></el-icon>
            <el-tag v-if="a.max_steps !== undefined" size="small" type="info" effect="plain">
              最多 {{ a.max_steps }} 步
            </el-tag>
          </div>
          <p class="muted agent-desc">{{ a.description }}</p>
          <div v-if="a.tools?.length" class="agent-tools">
            <el-tag v-for="t in a.tools" :key="t" size="small" effect="plain">{{ t }}</el-tag>
          </div>
        </el-card>
      </div>
    </el-card>

    <!-- 右区：发起运行 -->
    <el-card shadow="never">
      <template #header>
        <div class="page-head"><span class="card-title">发起运行</span></div>
      </template>

      <el-form label-position="top" @submit.prevent>
        <el-form-item label="智能体">
          <el-input :model-value="currentAgent" readonly placeholder="点击左侧智能体卡片选择" />
        </el-form-item>
        <el-form-item label="目标">
          <el-input
            v-model="goal"
            type="textarea"
            :rows="4"
            placeholder="用一句话描述这次要让智能体完成什么"
          />
        </el-form-item>
      </el-form>

      <el-button
        type="primary"
        :icon="VideoPlay"
        :loading="starting"
        :disabled="!currentAgent || !goal.trim()"
        @click="startRun"
      >
        {{ starting ? '发起中…' : '启动运行' }}
      </el-button>

      <el-alert
        v-if="runError"
        class="block-gap"
        type="error"
        :title="runError"
        show-icon
        :closable="false"
      />
    </el-card>
  </div>

  <!-- 运行详情：步骤时间线 -->
  <el-card v-if="run" class="block-gap" shadow="never">
    <template #header>
      <div class="page-head">
        <span class="card-title">运行详情</span>
        <StatusBadge :status="run.status" />
        <el-tag v-if="polling" size="small" type="warning" round>轮询中</el-tag>
      </div>
    </template>

    <el-descriptions :column="1" size="small">
      <el-descriptions-item label="运行 ID">
        <span class="mono">{{ run.run_id }}</span>
      </el-descriptions-item>
    </el-descriptions>

    <el-alert
      v-if="suspended()"
      class="block-gap"
      type="warning"
      show-icon
      :closable="false"
      title="存在待审批步骤，请到审批页处理"
    >
      <RouterLink class="alert-link" to="/approvals">前往审批页 →</RouterLink>
    </el-alert>

    <el-timeline v-if="run.steps?.length">
      <el-timeline-item
        v-for="s in run.steps"
        :key="s.step_index"
        :type="stepType(s.status)"
        hollow
      >
        <div class="step-line">
          <span class="step-no">步骤 {{ s.step_index }}</span>
          <strong class="mono">{{ s.tool }}</strong>
          <StatusBadge :status="s.status" />
          <el-tooltip v-if="s.trace_id" :content="s.trace_id" placement="top">
            <el-tag size="small" type="info" effect="plain" class="mono">
              trace {{ s.trace_id.slice(0, 10) }}…
            </el-tag>
          </el-tooltip>
        </div>
        <el-tooltip v-if="s.args !== undefined" :content="JSON.stringify(s.args)" placement="top">
          <p class="muted mono args">args {{ argsBrief(s.args) }}</p>
        </el-tooltip>
      </el-timeline-item>
    </el-timeline>
    <el-empty v-else :image-size="70" description="暂无步骤记录" />
  </el-card>
</template>

<style scoped>
.agent-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 60vh;
  overflow-y: auto;
}
/* 可点选卡片：选中态品牌描边 + 浅底 */
.agent-item {
  cursor: pointer;
  border-radius: var(--radius-sm);
  transition: border-color 0.15s ease, background 0.15s ease;
}
.agent-item :deep(.el-card__body) {
  padding: 12px 14px;
}
.agent-item.active {
  border-color: var(--brand);
  background: var(--brand-soft);
}
.agent-top {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.agent-icon {
  color: var(--brand);
}
.agent-name {
  font-size: 14px;
}
.agent-check {
  color: var(--brand);
  font-size: 14px;
}
.agent-desc {
  font-size: 13px;
  margin: 6px 0 0;
}
.agent-tools {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.step-line {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.step-no {
  color: var(--muted);
  font-size: 12px;
}
.args {
  font-size: 12px;
  margin: 2px 0 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 640px;
}
</style>