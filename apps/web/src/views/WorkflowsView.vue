<script setup lang="ts">
// 职责：编排页（Element Plus 版）—— 工作流列表 → 选中编辑（名称/说明/步骤：工具下拉选自真实
//       工具清单 + 参数 JSON + 上移/下移/删除）→ 保存（新建/更新）→ 执行 → 步骤时间线
//       （tool/status/耗时/远程出处/落单即停提示）；远程工具步骤天然可编排，执行走同一条
//       内核链，出站溯源随步返回，即本仓库的 RPA 联动形态
// 链路：router /workflows → api.listWorkflows/createWorkflow/updateWorkflow/deleteWorkflow/
//       runWorkflow + api.listTools（步骤工具下拉）；列表失败 → 壳红条，保存/执行失败 → 本页红条
// 对齐：AGENTS.md §4 前端红线（真实接口零 mock、禁直写 fetch、箭头函数、var(--*) token + scoped）+
//       PRD §2.11 可视化工作流编排 / §5.3 多场景串联与 RPA 联动
import { inject, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { ArrowDown, ArrowUp, Delete, Plus, VideoPlay } from '@element-plus/icons-vue'
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

const newBlank = (): WorkflowDetail => ({
  id: '',
  name: '',
  description: '',
  step_count: 0,
  created_by: '',
  updated_at: '',
  steps: [],
})

// 选中工作流进编辑态（详情含 steps 全文；新建态清空表单）
const pickWorkflow = async (id: string) => {
  pageError.value = ''
  run.value = null
  shell.clearError()
  try {
    const detail: WorkflowDetail = id ? await getWorkflow(id) : newBlank()
    editingId.value = id
    editName.value = detail.name
    editDesc.value = detail.description
    steps.value = detail.steps.map((s) => ({ tool: s.tool, argsText: stringify(s.args) }))
  } catch (e) {
    pageError.value = (e as Error).message || '工作流详情加载失败'
  }
}

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

// 时间线节点类型：失败红、挂起黄、成功绿、其余蓝
const stepType = (status: string) => {
  if (['failed', 'cancelled', 'error'].includes(status)) return 'danger'
  if (['pending', 'running'].includes(status)) return 'warning'
  if (['succeeded', 'completed', 'done'].includes(status)) return 'success'
  return 'primary'
}

onMounted(loadAll)
</script>

<template>
  <div class="grid">
    <!-- 左区：工作流列表 -->
    <el-card shadow="never">
      <template #header>
        <div class="page-head">
          <span class="card-title">工作流</span>
          <el-tag v-if="items.length" size="small" type="info" round>{{ items.length }} 条</el-tag>
        </div>
      </template>

      <el-button class="block-gap new-btn" type="primary" plain :icon="Plus" @click="pickWorkflow('')">
        新建工作流
      </el-button>

      <el-skeleton v-if="loading" :rows="4" animated />
      <el-empty
        v-else-if="!items.length"
        :image-size="80"
        description="暂无工作流（点新建编排第一条串联链）"
      />
      <div v-else class="wf-list">
        <el-card
          v-for="w in items"
          :key="w.id"
          :class="['wf-item', { active: w.id === editingId }]"
          shadow="hover"
          @click="pickWorkflow(w.id)"
        >
          <div class="wf-top">
            <strong>{{ w.name }}</strong>
            <el-tag size="small" effect="plain">{{ w.step_count }} 步</el-tag>
          </div>
          <p class="muted wf-desc">{{ w.description || '（无说明）' }}</p>
        </el-card>
      </div>
    </el-card>

    <!-- 右区：编辑器 -->
    <el-card shadow="never">
      <template #header>
        <div class="page-head">
          <span class="card-title">{{ editingId ? '编辑工作流' : '新建工作流' }}</span>
        </div>
      </template>

      <el-form label-position="top" @submit.prevent>
        <el-form-item label="名称">
          <el-input v-model="editName" placeholder="如：日报链（拉数→出报→建待办）" />
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="editDesc" placeholder="这条链解决什么场景" />
        </el-form-item>
      </el-form>

      <h3 class="sub-title">步骤（按序执行，需审批步骤落单即停）</h3>
      <el-empty v-if="!steps.length" :image-size="70" description="暂无步骤，先添加第一步" />
      <div class="step-list">
        <el-card v-for="(s, i) in steps" :key="i" class="step-card" shadow="never">
          <div class="step-head">
            <el-tag size="small" type="primary" effect="plain" round>步骤 {{ i + 1 }}</el-tag>
            <el-button-group class="step-ops">
              <el-button
                size="small"
                :icon="ArrowUp"
                :disabled="i === 0"
                title="上移"
                @click="moveStep(i, -1)"
              />
              <el-button
                size="small"
                :icon="ArrowDown"
                :disabled="i === steps.length - 1"
                title="下移"
                @click="moveStep(i, 1)"
              />
              <el-button size="small" :icon="Delete" title="删除" @click="removeStep(i)" />
            </el-button-group>
          </div>
          <el-form label-position="top" @submit.prevent>
            <el-form-item label="工具（下拉自真实工具清单，远程工具同样可选）">
              <el-select v-model="s.tool" style="width: 100%" filterable>
                <el-option v-for="t in toolNames" :key="t" :label="t" :value="t" />
              </el-select>
            </el-form-item>
            <el-form-item label="参数 JSON">
              <el-input
                v-model="s.argsText"
                type="textarea"
                :rows="3"
                spellcheck="false"
                class="args-input"
              />
            </el-form-item>
          </el-form>
        </el-card>
      </div>

      <div class="ops-row">
        <el-button :icon="Plus" @click="addStep">添加步骤</el-button>
        <el-button type="primary" :loading="saving" :disabled="!editName.trim()" @click="doSave">
          {{ saving ? '保存中…' : '保存' }}
        </el-button>
        <el-button
          v-if="editingId"
          type="success"
          :icon="VideoPlay"
          :loading="running"
          @click="doRun"
        >
          {{ running ? '执行中…' : '执行' }}
        </el-button>
        <el-popconfirm
          v-if="editingId"
          title="删除该工作流？只删定义，已产审批与审计不受影响。"
          confirm-button-text="删除"
          cancel-button-text="取消"
          @confirm="doDelete"
        >
          <template #reference>
            <el-button type="danger" plain :disabled="saving">删除</el-button>
          </template>
        </el-popconfirm>
      </div>

      <el-alert
        v-if="pageError"
        class="block-gap"
        type="error"
        :title="pageError"
        show-icon
        :closable="false"
      />
    </el-card>
  </div>

  <!-- 执行结果：步骤时间线 -->
  <el-card v-if="run" class="block-gap" shadow="never">
    <template #header>
      <div class="page-head">
        <span class="card-title">执行结果</span>
        <StatusBadge :status="run.status" />
      </div>
    </template>

    <el-alert
      v-if="run.status === 'pending_approval'"
      class="block-gap"
      type="warning"
      show-icon
      :closable="false"
      :title="`第 ${(run.next_step ?? 0) + 1} 步已提交审批，后续步骤暂不执行`"
    >
      <RouterLink class="alert-link" to="/approvals">前往审批页 →</RouterLink>
    </el-alert>
    <el-alert
      v-if="run.error"
      class="block-gap"
      type="error"
      show-icon
      :closable="false"
      :title="`第 ${(run.failed_step ?? 0) + 1} 步失败：${run.error}`"
    />

    <el-timeline v-if="run.steps.length">
      <el-timeline-item
        v-for="s in run.steps"
        :key="s.index"
        :type="stepType(s.status)"
        hollow
      >
        <div class="step-line">
          <span class="step-no">步骤 {{ s.index + 1 }}</span>
          <strong class="mono">{{ s.tool }}</strong>
          <StatusBadge :status="s.status" />
          <el-tag v-if="s.latency_ms !== undefined" size="small" effect="plain">
            {{ s.latency_ms }}ms
          </el-tag>
          <el-tag v-if="s.provider_id" size="small" type="primary" effect="plain">
            远程 · {{ s.provider_id }}
          </el-tag>
        </div>
        <pre v-if="s.result !== undefined" class="code-block step-result">{{ stringify(s.result) }}</pre>
      </el-timeline-item>
    </el-timeline>
    <el-empty v-else :image-size="70" description="无已完成步骤" />
  </el-card>
</template>

<style scoped>
.new-btn {
  width: 100%;
}
.wf-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 56vh;
  overflow-y: auto;
}
.wf-item {
  cursor: pointer;
  border-radius: var(--radius-sm);
  transition: border-color 0.15s ease, background 0.15s ease;
}
.wf-item :deep(.el-card__body) {
  padding: 12px 14px;
}
.wf-item.active {
  border-color: var(--brand);
  background: var(--brand-soft);
}
.wf-top {
  display: flex;
  align-items: center;
  gap: 8px;
}
.wf-desc {
  font-size: 13px;
  margin: 6px 0 0;
}
.step-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.step-card {
  background: #fafbfc;
  border-radius: var(--radius-sm);
}
.step-card :deep(.el-card__body) {
  padding: 12px 14px;
}
.step-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.step-ops {
  margin-left: auto;
}
.args-input :deep(textarea) {
  font-family: Consolas, 'Courier New', monospace;
  font-size: 13px;
}
.ops-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 14px;
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
.step-result {
  max-height: 200px;
  font-size: 12px;
}
</style>