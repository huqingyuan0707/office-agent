<script setup lang="ts">
// 职责：项目管理页（PRD §2.9 + §6）—— 台账查询（project.query 真查 + 简报）与任务拆解两步式
//       （decompose 出清单 → 页内微调责任人 → commit 批量建单恒送审），零 mock
// 链路：router /projects → api.invokeTool；对话微调走 /chat，页内核对后确认建单
// 对齐：PRD §6.2 用户操作步骤 + §6.5 二次确认约束 + AGENTS.md §4 前端红线
import { ref } from 'vue'
import { invokeTool } from '../api'

interface Project {
  name: string
  owner: string
  status: string
  progress: number
  current_milestone: string
  risks: { desc: string; level: string }[]
}
interface Subtask {
  id: string
  title: string
  deliverable: string
  owner: string
  due_date: string
  priority: string
  dependencies: string[]
}

// ---------- tab1：项目台账 ----------
const queryName = ref('')
const queryOwner = ref('')
const projects = ref<Project[]>([])
const brief = ref('')
const tab1Busy = ref(false)
const pageError = ref('')

const doQueryProjects = async () => {
  tab1Busy.value = true
  pageError.value = ''
  try {
    const data = await invokeTool('office.project.query', {
      ...(queryName.value.trim() ? { name: queryName.value.trim() } : {}),
      ...(queryOwner.value.trim() ? { owner: queryOwner.value.trim() } : {}),
    })
    const result = (data.result ?? {}) as { projects?: Project[]; brief?: string }
    projects.value = result.projects ?? []
    brief.value = result.brief ?? ''
  } catch (e) {
    projects.value = []
    pageError.value = (e as Error).message || '项目台账查询失败'
  } finally {
    tab1Busy.value = false
  }
}

// ---------- tab2：任务拆解（输入 → 清单 → 微调 → 一键同步）----------
const goal = ref('')
const duration = ref('')
const startDate = ref('')
const participants = ref('')
const subtasks = ref<Subtask[]>([])
const missingInfo = ref<string[]>([])
const commitHint = ref('')
const tab2Busy = ref(false)

const doDecompose = async () => {
  if (!goal.value.trim()) {
    pageError.value = '请先输入主任务目标'
    return
  }
  tab2Busy.value = true
  pageError.value = ''
  commitHint.value = ''
  try {
    const weeks = parseInt(duration.value.trim(), 10)
    const data = await invokeTool('office.task.decompose', {
      goal: goal.value.trim(),
      ...(Number.isFinite(weeks) && weeks > 0 ? { duration_weeks: weeks } : {}),
      ...(startDate.value.trim() ? { start_date: startDate.value.trim() } : {}),
      ...(participants.value.trim()
        ? { participants: participants.value.split(/[、,，\s]+/).filter(Boolean) }
        : {}),
    })
    const result = (data.result ?? {}) as { subtasks?: Subtask[]; missing_info?: string[] }
    subtasks.value = result.subtasks ?? []
    missingInfo.value = result.missing_info ?? []
  } catch (e) {
    subtasks.value = []
    pageError.value = (e as Error).message || '任务拆解失败'
  } finally {
    tab2Busy.value = false
  }
}

const doCommit = async () => {
  if (!subtasks.value.length) return
  pageError.value = ''
  commitHint.value = ''
  try {
    const data = await invokeTool('office.task.commit', {
      tasks: subtasks.value.map((t) => ({
        title: t.title,
        ...(t.owner.trim() ? { owner: t.owner.trim() } : {}),
        ...(t.due_date ? { due_date: t.due_date } : {}),
        priority: t.priority,
      })),
      idem_key: `web-${Date.now()}`,
    })
    const approvalId = (data as unknown as { approval_id?: string }).approval_id
    commitHint.value = approvalId
      ? `已提交审批（${approvalId}）：批量建单二次确认，批准后执行并通知责任人`
      : '已提交，请到审批页处理'
  } catch (e) {
    pageError.value = (e as Error).message || '批量建单失败'
  }
}
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="page-head">
        <span class="card-title">项目管理</span>
        <span class="head-hint">项目台账查询与一句话任务拆解（PRD §2.9 / §6）</span>
      </div>
    </template>
    <el-tabs>
      <el-tab-pane label="项目台账">
        <div class="toolbar">
          <el-input v-model="queryName" placeholder="搜索项目名" style="width: 200px" />
          <el-input v-model="queryOwner" placeholder="责任人" style="width: 140px" />
          <el-button type="primary" :loading="tab1Busy" @click="doQueryProjects">查询</el-button>
        </div>
        <el-table v-if="projects.length" :data="projects" stripe size="small" style="width: 100%">
          <el-table-column prop="name" label="项目" min-width="140" />
          <el-table-column prop="owner" label="责任人" width="110" />
          <el-table-column prop="status" label="状态" width="90" />
          <el-table-column prop="current_milestone" label="当前里程碑" min-width="160" show-overflow-tooltip />
          <el-table-column label="风险" min-width="160">
            <template #default="{ row }">
              <span class="muted">{{ (row.risks ?? []).map((r) => r.desc).join('；') || '无记录风险' }}</span>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-else :image-size="80" description="输入条件后查询（空结果如实展示）" />
        <p v-if="brief" class="muted">{{ brief }}</p>
      </el-tab-pane>

      <el-tab-pane label="任务拆解">
        <el-steps :active="subtasks.length ? 1 : 0" align-center finish-status="wait" class="block-gap">
          <el-step title="输入主任务" description="目标 / 工期 / 参与人" />
          <el-step title="拆解清单" description="交付物 / 依赖 / 优先级" />
          <el-step title="确认编辑" description="页内微调责任人" />
          <el-step title="一键同步" description="批量建单 + 通知" />
        </el-steps>
        <el-form label-width="72px" class="block-gap">
          <el-form-item label="主任务">
            <el-input v-model="goal" type="textarea" :rows="2" placeholder="如：官网改版项目" />
          </el-form-item>
          <el-form-item label="工期/启动">
            <el-input v-model="duration" placeholder="总工期（周，可空）" style="width: 180px" />
            <el-input v-model="startDate" placeholder="启动 YYYY-MM-DD（可选）" style="width: 220px" />
          </el-form-item>
          <el-form-item label="参与人">
            <el-input v-model="participants" placeholder="顿号/逗号/空格分隔，缺失会追问不臆造" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="tab2Busy" @click="doDecompose">开始拆解</el-button>
          </el-form-item>
        </el-form>
        <el-alert
          v-for="m in missingInfo"
          :key="m"
          class="block-gap"
          type="warning"
          :title="m"
          show-icon
          :closable="false"
        />
        <el-table v-if="subtasks.length" :data="subtasks" stripe size="small" style="width: 100%">
          <el-table-column prop="title" label="子任务" min-width="150" />
          <el-table-column prop="deliverable" label="交付物" width="140" show-overflow-tooltip />
          <el-table-column label="责任人（可改）" width="140">
            <template #default="{ row }">
              <el-input v-model="row.owner" size="small" placeholder="未指派" />
            </template>
          </el-table-column>
          <el-table-column prop="due_date" label="截止" width="120" />
          <el-table-column prop="priority" label="优先级" width="80" />
          <el-table-column label="前置依赖" width="100">
            <template #default="{ row }">{{ (row.dependencies ?? []).join(', ') || '—' }}</template>
          </el-table-column>
        </el-table>
        <div v-if="subtasks.length" class="toolbar block-gap">
          <el-button type="primary" @click="doCommit">确认并批量创建（需二次确认）</el-button>
        </div>
        <p v-if="commitHint" class="muted">{{ commitHint }}</p>
      </el-tab-pane>
    </el-tabs>
    <el-alert v-if="pageError" class="block-gap" type="error" :title="pageError" show-icon :closable="false" />
  </el-card>
</template>

<style scoped>
.toolbar {
  display: flex;
  gap: 12px;
  margin-bottom: 12px;
}
</style>
