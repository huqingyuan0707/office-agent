<script setup lang="ts">
// 职责：编排页 —— 工作流列表 → 选中编辑（名称/说明/步骤：工具下拉选自真实工具清单 +
//       参数 JSON + 上移/下移/删除）→ 保存（新建/更新）→ 执行 → 步骤时间线
//       （tool/status/耗时/远程出处/落单即停提示）；远程工具步骤天然可编排，执行走同一条
//       内核链，出站溯源随步返回，即本仓库的 RPA 联动形态
// 链路：router /workflows → api.listWorkflows/createWorkflow/updateWorkflow/deleteWorkflow/
//       runWorkflow + api.listTools（步骤工具下拉）；列表失败 → 壳红条，保存/执行失败 → 本页红条
// 对齐：AGENTS.md §4 前端红线（真实接口零 mock、禁直写 fetch、箭头函数、var(--*) token）+
//       PRD §2.11 可视化工作流编排 / §5.3 多场景串联与 RPA 联动
import { inject, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import {
  createWorkflow,
  deleteWorkflow,
  getWorkflow,
  listTools,
  listWorkflows,
  runWorkflow,
  updateWorkflow,
} from '../api'
import type { WorkflowDetail, WorkflowItem, WorkflowRun, WorkflowStep } from '../api'
import StatusBadge from '../components/StatusBadge.vue'

// 编辑态步骤（args 以文本编辑，保存时一次性 JSON 解析，非法即本页红条不提交）
interface StepEdit {
  tool: string
  argsText: string
}

const shell = inject('shellError') as {
  showError: (msg: string) => unknown
  clearError: () => unknown
}

const items = ref<WorkflowItem[]>([])
const loading = ref(true)
const toolNames = ref<string[]>([])
const editingId = ref('') // 空 = 新建态
const editName = ref('')
const editDesc = ref('')
const steps = ref<StepEdit[]>([])
const saving = ref(false)
const running = ref(false)
const run = ref<WorkflowRun | null>(null)
const pageError = ref('') // 保存 / 执行 / 解析失败红条（本页级，与壳红条分开）

const stringify = (value: unknown) => JSON.stringify(value, null, 2)

const loadAll = async () => {
  loading.value = true
  shell.clearError()
  try {
    items.value = await listWorkflows()
    toolNames.value = (await listTools()).items.map((t) => t.name)
  } catch (e) {
    items.value = []
    shell.showError((e as Error).message || '工作流列表加载失败')
  } finally {
    loading.value = false
  }
}

// 选中工作流进编辑态（详情含 steps 全文；新建态清空表单）
const pickWorkflow = async (id: string) => {
  pageError.value = ''
  run.value = null
  shell.clearError()
  try {
    const detail: WorkflowDetail = id ? await getWorkflow(id) : await Promise.resolve(newBlank())
    editingId.value = id
    editName.value = detail.name
    editDesc.value = detail.description
    steps.value = detail.steps.map((s) => ({ tool: s.tool, argsText: stringify(s.args) }))
  } catch (e) {
    pageError.value = (e as Error).message || '工作流详情加载失败'
  }
}

const newBlank = (): WorkflowDetail => ({
  id: '',
  name: '',
  description: '',
  step_count: 0,
  created_by: '',
  updated_at: '',
  steps: [],
})

// 步骤编辑：增 / 删 / 上移 / 下移（纯本地重排，保存才落库）
const addStep = () => {
  steps.value.push({ tool: toolNames.value[0] ?? '', argsText: '{}' })
}
const removeStep = (i: number) => {
  steps.value.splice(i, 1)
}
const moveStep = (i: number, delta: number) => {
  const j = i + delta
  if (j < 0 || j >= steps.value.length) return
  const row = steps.value[i]
  steps.value[i] = steps.value[j]
  steps.value[j] = row
}

// 保存：args 文本逐个 JSON 解析（非法即停，标出第几步，不提交半成品）
const collectSteps = (): WorkflowStep[] | null => {
  const out: WorkflowStep[] = []
  for (let i = 0; i < steps.value.length; i++) {
    let args: unknown
    try {
      args = JSON.parse(steps.value[i].argsText || '{}')
    } catch {
      pageError.value = `第 ${i + 1} 步的参数不是合法 JSON`
      return null
    }
    if (typeof args !== 'object' || args === null || Array.isArray(args)) {
      pageError.value = `第 ${i + 1} 步的 args 必须是键值对对象`
      return null
    }
    out.push({ tool: steps.value[i].tool, args: args as object })
  }
  return out
}

const doSave = async () => {
  if (!editName.value.trim() || saving.value) return
  const parsed = collectSteps()
  if (!parsed) return
  pageError.value = ''
  saving.value = true
  try {
    const saved = editingId.value
      ? await updateWorkflow(editingId.value, {
          name: editName.value.trim(),
          description: editDesc.value.trim(),
          steps: parsed,
        })
      : await createWorkflow(editName.value.trim(), editDesc.value.trim(), parsed)
    editingId.value = saved.id
    items.value = await listWorkflows()
  } catch (e) {
    pageError.value = (e as Error).message || '保存失败'
  } finally {
    saving.value = false
  }
}

const doDelete = async () => {
  if (!editingId.value || saving.value) return
  if (!confirm(`删除工作流「${editName.value}」？只删定义，已产审批与审计不受影响。`)) return
  pageError.value = ''
  saving.value = true
  try {
    await deleteWorkflow(editingId.value)
    editingId.value = ''
    editName.value = ''
    editDesc.value = ''
    steps.value = []
    run.value = null
    items.value = await listWorkflows()
  } catch (e) {
    pageError.value = (e as Error).message || '删除失败'
  } finally {
    saving.value = false
  }
}

const doRun = async () => {
  if (!editingId.value || running.value) return
  pageError.value = ''
  run.value = null
  running.value = true
  try {
    run.value = await runWorkflow(editingId.value)
  } catch (e) {
    pageError.value = (e as Error).message || '执行失败'
  } finally {
    running.value = false
  }
}

onMounted(loadAll)
</script>

<template>
  <div class="grid">
    <!-- 左区：工作流列表 -->
    <section class="card">
      <h2>
        工作流
        <span v-if="items.length" class="badge">{{ items.length }}</span>
      </h2>
      <button class="btn primary block-gap" @click="pickWorkflow('')">+ 新建工作流</button>
      <p v-if="loading" class="empty">加载中…</p>
      <div v-else-if="!items.length" class="empty">暂无工作流（点新建编排第一条串联链）</div>
      <template v-else>
        <div
          v-for="w in items"
          :key="w.id"
          :class="['tool-card', { active: w.id === editingId }]"
          @click="pickWorkflow(w.id)"
        >
          <div class="tool-head">
            <strong>{{ w.name }}</strong>
            <span class="badge">{{ w.step_count }} 步</span>
          </div>
          <p class="muted">{{ w.description || '（无说明）' }}</p>
        </div>
      </template>
    </section>

    <!-- 右区：编辑器 -->
    <section class="card">
      <h2>{{ editingId ? '编辑工作流' : '新建工作流' }}</h2>
      <label class="field">
        <span>名称</span>
        <input v-model="editName" placeholder="如：日报链（拉数→出报→建待办）" />
      </label>
      <label class="field">
        <span>说明</span>
        <input v-model="editDesc" placeholder="这条链解决什么场景" />
      </label>

      <h3 class="muted">步骤（按序执行，需审批步骤落单即停）</h3>
      <div v-if="!steps.length" class="empty">暂无步骤，先添加第一步</div>
      <div v-for="(s, i) in steps" :key="i" class="card step-card">
        <div class="tool-head">
          <span class="badge">步骤 {{ i + 1 }}</span>
          <button class="btn ghost" :disabled="i === 0" @click="moveStep(i, -1)">上移</button>
          <button class="btn ghost" :disabled="i === steps.length - 1" @click="moveStep(i, 1)">
            下移
          </button>
          <button class="btn ghost" @click="removeStep(i)">删除</button>
        </div>
        <label class="field">
          <span>工具（下拉自真实工具清单，远程工具同样可选）</span>
          <select v-model="s.tool">
            <option v-for="t in toolNames" :key="t" :value="t">{{ t }}</option>
          </select>
        </label>
        <label class="field">
          <span>参数 JSON</span>
          <textarea v-model="s.argsText" rows="3" spellcheck="false"></textarea>
        </label>
      </div>
      <div class="tool-head block-gap">
        <button class="btn" @click="addStep">+ 添加步骤</button>
        <button class="btn primary" :disabled="saving || !editName.trim()" @click="doSave">
          {{ saving ? '保存中…' : '保存' }}
        </button>
        <button v-if="editingId" class="btn" :disabled="running" @click="doRun">
          {{ running ? '执行中…' : '执行' }}
        </button>
        <button v-if="editingId" class="btn ghost" :disabled="saving" @click="doDelete">删除</button>
      </div>

      <div v-if="pageError" class="result fail">{{ pageError }}</div>
    </section>
  </div>

  <!-- 执行结果：步骤时间线 -->
  <section v-if="run" class="card block-gap">
    <h2>
      执行结果
      <span class="badge">{{ run.status }}</span>
    </h2>
    <div v-if="run.status === 'pending_approval'" class="err-bar">
      <span class="err-text">第 {{ (run.next_step ?? 0) + 1 }} 步已提交审批，后续步骤暂不执行</span>
      <RouterLink class="err-link" to="/approvals">前往审批页 →</RouterLink>
    </div>
    <p v-if="run.error" class="result fail">第 {{ (run.failed_step ?? 0) + 1 }} 步失败：{{ run.error }}</p>
    <div v-if="run.steps.length" class="timeline">
      <div v-for="s in run.steps" :key="s.index" class="timeline-item">
        <div class="tool-head">
          <span class="badge">步骤 {{ s.index + 1 }}</span>
          <strong class="mono">{{ s.tool }}</strong>
          <StatusBadge :status="s.status" />
          <span v-if="s.latency_ms !== undefined" class="badge">{{ s.latency_ms }}ms</span>
          <span v-if="s.provider_id" class="badge">远程 · {{ s.provider_id }}</span>
        </div>
        <pre v-if="s.result !== undefined" class="args-brief">{{ stringify(s.result) }}</pre>
      </div>
    </div>
    <p v-else class="empty">无已完成步骤</p>
  </section>
</template>
