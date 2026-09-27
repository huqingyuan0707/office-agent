<script setup lang="ts">
// 职责：会议协作页（PRD §2.5 会议智能协作）——真实接线：会前议程/资料包/预约、会中要点梳理、会后落实跟进、风险预警
// 链路：router /meetings → 本页 → invokeTool office.meeting.* / office.meeting_flow.*
// 写动作恒送审：meeting.book 由后端审批闸门把关，前端拿到 approval_id 如实提示
// 对齐：PRD §2.5 + AGENTS.md §4 前端红线（禁 mock 兜底，失败置空 + 报错提示）
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { invokeTool } from '../api'

interface RiskHit {
  keyword: string
  level: string
  excerpt: string
}
interface FollowupItem {
  task: string
  owner?: string
  due?: string
  state: string
  state_label: string
  todo_id?: string
}

const agendaForm = reactive({
  title: '',
  objective: '',
  topics: [] as string[],
  attendees: [] as string[],
  duration_minutes: 60,
})
const agendaText = ref('')
const agendaLoading = ref(false)

const bookForm = reactive({
  title: '',
  start_time: '',
  duration_minutes: 60,
  attendees: [] as string[],
  agenda: '',
})
const booking = ref(false)

const materialsForm = reactive({
  title: '',
  files: [] as string[],
  topics: [] as string[],
})
const packText = ref('')
const packStats = ref<{ file_total: number; file_ok: number } | null>(null)
const materialsLoading = ref(false)

const digestForm = reactive({ title: '', notes: '' })
const digestText = ref('')
const digestCounts = ref<{ decision: number; action: number; risk: number } | null>(null)
const digestLoading = ref(false)

const followupItems = ref<FollowupItem[]>([])
const followupText = ref('')
const followupCounts = ref<Record<string, number> | null>(null)
const followupForm = reactive({
  lines: [] as { task: string; owner: string; due: string }[],
})
const followupLoading = ref(false)

const riskForm = reactive({ minutes: '' })
const risks = ref<RiskHit[]>([])
const riskLoading = ref(false)

const _listen = (res: unknown, field: string) => {
  if (!res || typeof res !== 'object' || (res as { status?: string }).status !== 'ok') {
    throw new Error(res !== undefined && res !== null ? String((res as { result?: unknown }).result ?? res) : '未返回结果')
  }
  const payload = (res as { result?: unknown }).result as Record<string, unknown> | undefined
  if (!payload) return null
  if (payload[field] !== undefined) return String(payload[field] ?? '')
  return null
}
const _listenObj = <T>(res: unknown, field: string): T | null => {
  if (!res || typeof res !== 'object' || (res as { status?: string }).status !== 'ok') return null
  const payload = (res as { result?: unknown }).result as Record<string, unknown> | undefined
  return (payload?.[field] as T) ?? null
}

const genAgenda = async () => {
  if (!agendaForm.title.trim()) {
    ElMessage.warning('请填写会议主题')
    return
  }
  agendaLoading.value = true
  try {
    const res = await invokeTool('office.meeting.agenda', {
      title: agendaForm.title.trim(),
      objective: agendaForm.objective || undefined,
      topics: agendaForm.topics,
      attendees: agendaForm.attendees,
      duration_minutes: agendaForm.duration_minutes,
    })
    agendaText.value = _listen(res, 'agenda') ?? ''
    if (!agendaText.value) ElMessage.error('议程生成未返回内容')
  } catch (e) {
    agendaText.value = ''
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    agendaLoading.value = false
  }
}

const bookMeeting = async () => {
  if (!bookForm.title.trim() || !bookForm.start_time) {
    ElMessage.warning('请填写会议主题与开始时间')
    return
  }
  booking.value = true
  try {
    const res = await invokeTool('office.meeting.book', {
      title: bookForm.title.trim(),
      start_time: bookForm.start_time,
      duration_minutes: bookForm.duration_minutes,
      attendees: bookForm.attendees,
      agenda: bookForm.agenda || undefined,
    })
    if (res.requires_approval) {
      ElMessage.info(`会议预约已提交审批（单号 ${res.approval_id || '-'}），复核员批准后执行`)
    } else {
      ElMessage.success('会议预约成功')
    }
    bookForm.title = ''
    bookForm.start_time = ''
    bookForm.attendees = []
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    booking.value = false
  }
}

const genMaterials = async () => {
  if (!materialsForm.title.trim() || !materialsForm.files.length) {
    ElMessage.warning('请填写会议主题并至少选择一个资料文件')
    return
  }
  materialsLoading.value = true
  try {
    const res = await invokeTool('office.meeting.materials', {
      title: materialsForm.title.trim(),
      files: materialsForm.files,
      topics: materialsForm.topics,
    })
    packText.value = _listen(res, 'pack') ?? ''
    packStats.value = _listenObj<{ file_total: number; file_ok: number }>(res, 'counts')
    if (!packText.value) ElMessage.error('资料包汇编未返回内容')
  } catch (e) {
    packText.value = ''
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    materialsLoading.value = false
  }
}

const genDigest = async () => {
  if (!digestForm.notes.trim()) {
    ElMessage.warning('请粘贴会议速记/转录文本')
    return
  }
  digestLoading.value = true
  try {
    const res = await invokeTool('office.meeting.digest', {
      title: digestForm.title || undefined,
      notes: digestForm.notes,
    })
    digestText.value = _listen(res, 'digest') ?? ''
    digestCounts.value = _listenObj<{ decision: number; action: number; risk: number }>(res, 'counts')
    if (!digestText.value) ElMessage.error('要点梳理未返回内容')
  } catch (e) {
    digestText.value = ''
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    digestLoading.value = false
  }
}

const addFollowupRow = () => {
  followupForm.lines.push({ task: '', owner: '', due: '' })
}
const removeFollowupRow = (i: number) => {
  followupForm.lines.splice(i, 1)
}
const runFollowup = async () => {
  const items = followupForm.lines.filter((l) => l.task.trim())
  if (!items.length) {
    ElMessage.warning('请至少添加一条行动项（事项必填）')
    return
  }
  followupLoading.value = true
  try {
    const res = await invokeTool('office.meeting.followup', {
      action_items: items.map((it) => ({
        task: it.task.trim(),
        owner: it.owner.trim() || undefined,
        due: it.due || undefined,
      })),
    })
    followupItems.value = _listenObj<FollowupItem[]>(res, 'items') ?? []
    followupText.value = _listen(res, 'followup') ?? ''
    followupCounts.value = _listenObj<Record<string, number>>(res, 'counts')
    if (!followupItems.value.length) ElMessage.error('落实跟进未返回结果')
  } catch (e) {
    followupItems.value = []
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    followupLoading.value = false
  }
}

const scanRisks = async () => {
  if (!riskForm.minutes.trim()) {
    ElMessage.warning('请粘贴纪要文本')
    return
  }
  riskLoading.value = true
  try {
    const res = await invokeTool('office.meeting.risks', { minutes: riskForm.minutes })
    risks.value = _listenObj<RiskHit[]>(res, 'risks') ?? []
    if (!risks.value.length) {
      ElMessage.info(_listen(res, 'degraded_reason') || '未命中风险关键词，不编造风险')
    }
  } catch (e) {
    risks.value = []
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    riskLoading.value = false
  }
}
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="page-head">
        <span class="card-title">会议协作</span>
        <span class="head-hint">覆盖会前准备、会中记录、会后纪要与风险预警全流程（PRD §2.5）</span>
      </div>
    </template>
    <el-steps :active="0" align-center finish-status="wait" class="block-gap">
      <el-step title="会前" description="预约 · 资料 · 议程" />
      <el-step title="会中" description="速记 · 要点梳理" />
      <el-step title="会后" description="行动项 · 跟进对账" />
      <el-step title="风险" description="风险点 · 预警" />
    </el-steps>
    <el-tabs>
      <el-tab-pane label="会前准备">
        <el-form label-width="100px" style="max-width: 640px">
          <el-form-item label="会议主题">
            <el-input v-model="agendaForm.title" placeholder="如：Q4 产品评审" />
          </el-form-item>
          <el-form-item label="会议目标">
            <el-input v-model="agendaForm.objective" type="textarea" :rows="2" placeholder="本次会议期望达成的目标" />
          </el-form-item>
          <el-form-item label="议题列表">
            <el-select v-model="agendaForm.topics" multiple filterable allow-create default-first-option placeholder="输入议题后回车" style="width: 100%" />
          </el-form-item>
          <el-form-item label="参会人员">
            <el-select v-model="agendaForm.attendees" multiple filterable allow-create default-first-option placeholder="输入参会人用户名" style="width: 100%" />
          </el-form-item>
          <el-form-item label="预计时长">
            <el-input-number v-model="agendaForm.duration_minutes" :min="5" :max="480" :step="5" />
            <span class="muted" style="margin-left: 8px">分钟</span>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="agendaLoading" @click="genAgenda">生成会议议程</el-button>
          </el-form-item>
        </el-form>
        <pre v-if="agendaText" class="result-block">{{ agendaText }}</pre>

        <el-divider content-position="left">整理会议资料</el-divider>
        <el-form label-width="100px" style="max-width: 640px">
          <el-form-item label="会议主题">
            <el-input v-model="materialsForm.title" placeholder="如：Q4 产品评审" />
          </el-form-item>
          <el-form-item label="资料文件">
            <el-select v-model="materialsForm.files" multiple filterable allow-create default-first-option placeholder="DOCS_DIR 内文件名（txt/md/pdf/docx/xlsx 等）" style="width: 100%" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="materialsLoading" @click="genMaterials">汇编会议资料包</el-button>
          </el-form-item>
        </el-form>
        <p v-if="packStats" class="muted">资料 {{ packStats.file_total }} 份，成功抽取 {{ packStats.file_ok }} 份（失败项见正文原因）</p>
        <pre v-if="packText" class="result-block">{{ packText }}</pre>

        <el-divider content-position="left">发起会议预约（送审）</el-divider>
        <el-form label-width="100px" style="max-width: 640px">
          <el-form-item label="会议主题">
            <el-input v-model="bookForm.title" placeholder="如：Q4 产品评审" />
          </el-form-item>
          <el-form-item label="开始时间">
            <el-date-picker v-model="bookForm.start_time" type="datetime" :format="'YYYY-MM-DD HH:mm'" :value-format="'YYYY-MM-DD HH:mm'" placeholder="选择开始时间" style="width: 100%" />
          </el-form-item>
          <el-form-item label="时长(分钟)">
            <el-input-number v-model="bookForm.duration_minutes" :min="5" :max="480" :step="5" />
          </el-form-item>
          <el-form-item label="参会人员">
            <el-select v-model="bookForm.attendees" multiple filterable allow-create default-first-option placeholder="输入参会人用户名" style="width: 100%" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="booking" @click="bookMeeting">预约并发送邀请</el-button>
          </el-form-item>
        </el-form>
      </el-tab-pane>

      <el-tab-pane label="会中记录">
        <el-alert type="info" :closable="false" title="实时语音转录依赖音频基建（已后置）；此处承接「速记/转录文本 → 要点梳理」这一段" class="block-gap" />
        <el-form label-width="100px">
          <el-form-item label="会议主题">
            <el-input v-model="digestForm.title" placeholder="可选" style="max-width: 360px" />
          </el-form-item>
          <el-form-item label="速记文本">
            <el-input v-model="digestForm.notes" type="textarea" :rows="8" placeholder="粘贴会议速记 / 转录文本，按页面内容逐行归类决议/行动项/风险要点" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="digestLoading" @click="genDigest">梳理会议要点</el-button>
          </el-form-item>
        </el-form>
        <div v-if="digestCounts" class="block-gap">
          <el-tag type="success">决议 {{ digestCounts.decision }}</el-tag>
          <el-tag type="primary" style="margin-left: 8px">行动项 {{ digestCounts.action }}</el-tag>
          <el-tag type="danger" style="margin-left: 8px">风险 {{ digestCounts.risk }}</el-tag>
        </div>
        <pre v-if="digestText" class="result-block">{{ digestText }}</pre>
      </el-tab-pane>

      <el-tab-pane label="会后纪要">
        <el-form label-width="100px" style="max-width: 640px">
          <el-form-item>
            <div style="width: 100%">
              <div v-for="(row, i) in followupForm.lines" :key="i" class="followup-row">
                <el-input v-model="row.task" placeholder="行动事项（必填）" style="flex: 2" />
                <el-input v-model="row.owner" placeholder="责任人" style="flex: 1" />
                <el-date-picker v-model="row.due" type="date" :value-format="'YYYY-MM-DD'" placeholder="截止" style="flex: 1" />
                <el-button link type="danger" @click="removeFollowupRow(i)">删除</el-button>
              </div>
              <el-button link type="primary" @click="addFollowupRow">+ 添加行动项</el-button>
            </div>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="followupLoading" @click="runFollowup">与待办台账对账跟进</el-button>
          </el-form-item>
        </el-form>
        <template v-if="followupItems.length">
          <div class="block-gap">
            <el-tag v-for="(v, k) in followupCounts" :key="k" style="margin-right: 8px">
              {{ { done: '已完成', overdue: '已逾期', open: '进行中', cancelled: '已取消', missing: '未建单' }[k] ?? k }}：{{ v }}
            </el-tag>
          </div>
          <el-table :data="followupItems">
            <el-table-column prop="task" label="行动事项" min-width="200" />
            <el-table-column prop="owner" label="责任人" width="120" />
            <el-table-column prop="due" label="期限" width="120" />
            <el-table-column label="状态" width="110">
              <template #default="{ row }">
                <el-tag :type="row.state === 'done' ? 'success' : row.state === 'overdue' ? 'danger' : row.state === 'cancelled' ? 'info' : row.state === 'missing' ? 'warning' : 'primary'">
                  {{ row.state_label }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
        </template>
        <pre v-if="followupText" class="result-block">{{ followupText }}</pre>
      </el-tab-pane>

      <el-tab-pane label="风险预警">
        <el-form label-width="100px">
          <el-form-item label="纪要文本">
            <el-input v-model="riskForm.minutes" type="textarea" :rows="8" placeholder="粘贴会议纪要 / 要点文本，按风险关键词规则摘录命中项" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="riskLoading" @click="scanRisks">扫描风险点</el-button>
          </el-form-item>
        </el-form>
        <el-table v-if="risks.length" :data="risks">
          <el-table-column prop="keyword" label="风险关键词" width="120" />
          <el-table-column label="级别" width="100">
            <template #default="{ row }">
              <el-tag :type="row.level === '高' ? 'danger' : row.level === '中' ? 'warning' : 'info'">{{ row.level }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="excerpt" label="原文摘录" min-width="320" />
        </el-table>
      </el-tab-pane>
    </el-tabs>
  </el-card>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.result-block {
  margin: 0;
  padding: 12px 16px;
  background: var(--el-fill-color-light);
  border-radius: 6px;
  white-space: pre-wrap;
  line-height: 1.8;
  font-family: var(--el-font-family);
}
.followup-row {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}
</style>