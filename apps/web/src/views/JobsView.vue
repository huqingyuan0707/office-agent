<script setup lang="ts">
// 职责：定时任务页（PRD §2.11 高阶自动化·定时任务）——作业列表（类型/调度/下次执行/最近结果）
//       + 新建对话框（notify_scan 通知扫描 / workflow_run 绑定真实工作流；daily/weekly/interval
//       三形态调度）+ 启停 / 立即执行 / 删除 / 手动扫描到点（tick，仅 admin，非 admin 如实 403）
// 链路：router /jobs → api.listJobs/createJob/updateJob/deleteJob/runJob/tickJobs
//       + api.listWorkflows（workflow_run 下拉选自真实编排清单）；列表失败 → 壳红条，
//       操作失败 → 本页红条；一切数据来自后端真实接口，零 mock
// 对齐：AGENTS.md §4 前端红线 + PRD §2.11（定时任务/场景串联）
import { inject, onMounted, ref } from 'vue'
import { Delete, Plus, VideoPlay } from '@element-plus/icons-vue'
import {
  createJob,
  deleteJob,
  listJobs,
  listWorkflows,
  runJob,
  tickJobs,
  updateJob,
} from '../api'
import type { JobItem, JobSchedule, WorkflowItem } from '../api'

const shell = inject('shellError') as {
  showError: (msg: string) => unknown
  clearError: () => unknown
}

const WEEK_DAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
const TYPE_LABELS: Record<string, string> = {
  notify_scan: '通知扫描',
  workflow_run: '工作流执行',
}

const items = ref<JobItem[]>([])
const workflows = ref<WorkflowItem[]>([])
const loading = ref(true)
const busyId = ref('')
const pageError = ref('')
const lastTick = ref('')

const dialogOpen = ref(false)
const formName = ref('')
const formType = ref<'notify_scan' | 'workflow_run'>('notify_scan')
const formKind = ref<'daily' | 'weekly' | 'interval'>('daily')
const formAt = ref('09:00')
const formDay = ref(0)
const formSeconds = ref(3600)
const formWorkflowId = ref('')
const creating = ref(false)

const scheduleText = (s: JobSchedule) => {
  if (s.kind === 'interval') return `每 ${s.seconds} 秒`
  if (s.kind === 'weekly') return `每${WEEK_DAYS[s.day ?? 0]} ${s.at}`
  return `每天 ${s.at}`
}

// el-table 列插槽的 row 被 EP 推断为 DefaultRow，与后端返回项同构；此处收窄回领域类型
const resultText = (raw: unknown) => {
  const row = raw as JobItem
  if (!row.last_result) return '（尚未执行）'
  try {
    const parsed = JSON.parse(row.last_result) as { status?: string; error?: string }
    if (parsed.status && parsed.status !== 'ok') return `${parsed.status}：${parsed.error || ''}`
    return parsed.status === 'ok' ? 'ok' : row.last_result.slice(0, 80)
  } catch {
    return row.last_result.slice(0, 80)
  }
}

const workflowName = (raw: unknown) => {
  const row = raw as JobItem
  const id = String(row.payload?.workflow_id ?? '')
  return workflows.value.find((w) => w.id === id)?.name || id || '（未绑定）'
}

const loadAll = async () => {
  loading.value = true
  shell.clearError()
  try {
    items.value = await listJobs()
    workflows.value = await listWorkflows()
  } catch (e) {
    items.value = []
    shell.showError((e as Error).message || '定时任务列表加载失败')
  } finally {
    loading.value = false
  }
}

const openCreate = () => {
  pageError.value = ''
  lastTick.value = ''
  formName.value = ''
  formType.value = 'notify_scan'
  formKind.value = 'daily'
  formAt.value = '09:00'
  formDay.value = 0
  formSeconds.value = 3600
  formWorkflowId.value = workflows.value[0]?.id ?? ''
  dialogOpen.value = true
}

const buildSchedule = (): JobSchedule | null => {
  if (formKind.value === 'interval') {
    if (!Number.isInteger(formSeconds.value) || formSeconds.value < 30) {
      pageError.value = '执行间隔需为不小于 30 的整数秒'
      return null
    }
    return { kind: 'interval', seconds: formSeconds.value }
  }
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(formAt.value)) {
    pageError.value = '执行时间需为 HH:MM 两位补零格式（如 08:30）'
    return null
  }
  if (formKind.value === 'weekly') return { kind: 'weekly', day: formDay.value, at: formAt.value }
  return { kind: 'daily', at: formAt.value }
}

const submitCreate = async () => {
  pageError.value = ''
  const schedule = buildSchedule()
  if (!schedule) return
  if (formType.value === 'workflow_run' && !formWorkflowId.value) {
    pageError.value = '请先创建工作流或选择通知扫描类型'
    return
  }
  creating.value = true
  try {
    await createJob(
      formName.value,
      formType.value,
      schedule,
      formType.value === 'workflow_run' ? { workflow_id: formWorkflowId.value } : {}
    )
    dialogOpen.value = false
    await loadAll()
  } catch (e) {
    pageError.value = (e as Error).message || '创建失败'
  } finally {
    creating.value = false
  }
}

const toggleEnabled = async (raw: unknown) => {
  const row = raw as JobItem
  busyId.value = row.id
  pageError.value = ''
  try {
    await updateJob(row.id, { enabled: !row.enabled })
    await loadAll()
  } catch (e) {
    pageError.value = (e as Error).message || '启停失败'
  } finally {
    busyId.value = ''
  }
}

const runNow = async (raw: unknown) => {
  const row = raw as JobItem
  busyId.value = row.id
  pageError.value = ''
  try {
    const result = await runJob(row.id)
    pageError.value =
      result.status === 'ok'
        ? ''
        : `「${row.name}」执行结果：${result.status}${result.error ? `——${result.error}` : ''}`
    await loadAll()
  } catch (e) {
    pageError.value = (e as Error).message || '执行失败'
  } finally {
    busyId.value = ''
  }
}

const removeJob = async (raw: unknown) => {
  const row = raw as JobItem
  busyId.value = row.id
  pageError.value = ''
  try {
    await deleteJob(row.id)
    await loadAll()
  } catch (e) {
    pageError.value = (e as Error).message || '删除失败'
  } finally {
    busyId.value = ''
  }
}

const doTick = async () => {
  pageError.value = ''
  lastTick.value = ''
  try {
    const result = await tickJobs()
    lastTick.value = `本轮到点执行 ${result.executed} 个作业`
    await loadAll()
  } catch (e) {
    pageError.value = (e as Error).message || '扫描失败（手动扫描仅管理员可用）'
  }
}

onMounted(loadAll)
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="page-head">
        <span class="card-title">定时任务</span>
        <span class="head-hint"
          >通知扫描链与工作流按登记计划自动执行（PRD §2.11）；执行身份为创建人实时角色</span
        >
      </div>
    </template>
    <div class="toolbar">
      <el-button type="primary" :icon="Plus" @click="openCreate">新建定时任务</el-button>
      <el-button @click="doTick">扫描到点作业</el-button>
      <el-button :loading="loading" @click="loadAll">刷新</el-button>
      <span v-if="lastTick" class="tick-hint">{{ lastTick }}</span>
    </div>
    <el-alert v-if="pageError" :title="pageError" type="error" show-icon closable />
    <el-table v-loading="loading" :data="items" empty-text="暂无定时任务，点「新建定时任务」登记">
      <el-table-column prop="name" label="任务名" min-width="140" />
      <el-table-column label="类型" width="110">
        <template #default="{ row }">{{ TYPE_LABELS[row.job_type] || row.job_type }}</template>
      </el-table-column>
      <el-table-column label="调度周期" width="140">
        <template #default="{ row }">{{ scheduleText(row.schedule) }}</template>
      </el-table-column>
      <el-table-column label="执行内容" min-width="140">
        <template #default="{ row }">{{
          row.job_type === 'workflow_run' ? workflowName(row) : '通知扫描（幂等去重）'
        }}</template>
      </el-table-column>
      <el-table-column prop="next_run_at" label="下次执行" width="160" />
      <el-table-column label="最近结果" min-width="150" show-overflow-tooltip>
        <template #default="{ row }">
          <span :class="row.last_result.includes('failed') ? 'result-failed' : ''">{{
            resultText(row)
          }}</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
            {{ row.enabled ? '启用' : '停用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="230" fixed="right">
        <template #default="{ row }">
          <el-button
            size="small"
            :icon="VideoPlay"
            :loading="busyId === row.id"
            @click="runNow(row)"
            >执行</el-button
          >
          <el-button size="small" :loading="busyId === row.id" @click="toggleEnabled(row)">{{
            row.enabled ? '停用' : '启用'
          }}</el-button>
          <el-button size="small" type="danger" :icon="Delete" @click="removeJob(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-card>

  <el-dialog v-model="dialogOpen" title="新建定时任务" width="520px">
    <el-form label-width="90px">
      <el-form-item label="任务名">
        <el-input v-model="formName" maxlength="80" placeholder="如：每日 09:00 简报扫描" />
      </el-form-item>
      <el-form-item label="类型">
        <el-select v-model="formType" style="width: 100%">
          <el-option label="通知扫描（审批超时/待办到期/简报等七类信号）" value="notify_scan" />
          <el-option label="工作流执行（按编排顺序跑，写步骤仍走审批闸门）" value="workflow_run" />
        </el-select>
      </el-form-item>
      <el-form-item v-if="formType === 'workflow_run'" label="工作流">
        <el-select v-model="formWorkflowId" style="width: 100%" placeholder="选自真实编排清单">
          <el-option v-for="w in workflows" :key="w.id" :label="w.name" :value="w.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="调度方式">
        <el-radio-group v-model="formKind">
          <el-radio-button value="daily">每天</el-radio-button>
          <el-radio-button value="weekly">每周</el-radio-button>
          <el-radio-button value="interval">固定间隔</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item v-if="formKind === 'weekly'" label="星期">
        <el-select v-model="formDay" style="width: 140px">
          <el-option v-for="(d, i) in WEEK_DAYS" :key="d" :label="d" :value="i" />
        </el-select>
      </el-form-item>
      <el-form-item v-if="formKind !== 'interval'" label="执行时间">
        <el-input v-model="formAt" style="width: 140px" placeholder="08:30" />
        <span class="form-hint">业务时区（Asia/Shanghai）</span>
      </el-form-item>
      <el-form-item v-if="formKind === 'interval'" label="间隔秒数">
        <el-input-number v-model="formSeconds" :min="30" :step="60" />
        <span class="form-hint">不小于 30 秒</span>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogOpen = false">取消</el-button>
      <el-button type="primary" :loading="creating" @click="submitCreate">创建</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.toolbar {
  display: flex;
  gap: 12px;
  margin-bottom: 12px;
  align-items: center;
}
.tick-hint {
  color: var(--el-color-success, var(--text-muted));
  font-size: 13px;
}
.form-hint {
  margin-left: 10px;
  color: var(--text-muted);
  font-size: 12px;
}
.result-failed {
  color: var(--el-color-danger);
}
</style>
