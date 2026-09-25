<script setup lang="ts">
// 职责：人事行政页（PRD §2.8）—— 考勤加班查询、入离职清单生成、办公资源占用查询与预订，
//       全部走后端真实工具（hr.attendance/checklist、resource.query/book），零 mock
// 链路：router /hr → api.invokeTool；预订是写动作恒送审（落单后到审批页批准）；失败本页红条 + 置空
// 对齐：AGENTS.md §4 前端红线（真实接口零 mock、箭头函数、var(--*) token + scoped）+ PRD §2.8
import { ref } from 'vue'
import { invokeTool } from '../api'

interface Attendance {
  status_count?: Record<string, number>
  work_days?: number
  total_hours?: number
  overtime_hours?: number
  overtime_rule?: string
  attendance?: { date?: string; status?: string }[]
}
interface Checklist {
  sections?: string[]
  reminders?: string[]
  unfilled?: string[]
}
interface ResourceSlot {
  date?: string
  start?: string
  end?: string
  purpose?: string
  by?: string
}
interface Resource {
  id: string
  type: string
  name: string
  capacity?: number
  booked_slots?: ResourceSlot[]
}

// ---------- tab1：考勤加班 ----------
const person = ref('')
const month = ref('')
const attendance = ref<Attendance | null>(null)
const tab1Busy = ref(false)
const pageError = ref('')

const doAttendance = async () => {
  if (!person.value.trim()) return
  tab1Busy.value = true
  pageError.value = ''
  try {
    const data = await invokeTool('office.hr.attendance', {
      person: person.value.trim(),
      ...(month.value.trim() ? { month: month.value.trim() } : {}),
    })
    attendance.value = (data.result ?? {}) as Attendance
  } catch (e) {
    attendance.value = null
    pageError.value = (e as Error).message || '考勤查询失败'
  } finally {
    tab1Busy.value = false
  }
}

// ---------- tab2：入离职清单 ----------
const scene = ref<string>('onboard')
const checklistPerson = ref('')
const handoverText = ref('')
const checklist = ref<Checklist | null>(null)
const tab2Busy = ref(false)

const doChecklist = async () => {
  tab2Busy.value = true
  pageError.value = ''
  try {
    const handover = handoverText.value
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => {
        const [item = '', owner = '', due = ''] = line.split('|')
        return { item: item.trim(), owner: owner.trim() || null, due: due.trim() || null }
      })
      .filter((h) => h.item)
    const data = await invokeTool('office.hr.checklist', {
      scene: scene.value,
      ...(checklistPerson.value.trim() ? { person: checklistPerson.value.trim() } : {}),
      ...(scene.value === 'offboard' && handover.length ? { handover } : {}),
    })
    checklist.value = (data.result ?? {}) as Checklist
  } catch (e) {
    checklist.value = null
    pageError.value = (e as Error).message || '清单生成失败'
  } finally {
    tab2Busy.value = false
  }
}

// ---------- tab3：资源占用 + 预订 ----------
const resourceType = ref<string>('')
const resourceDate = ref('')
const resources = ref<Resource[]>([])
const tab3Busy = ref(false)
const bookResourceId = ref('')
const bookStart = ref('')
const bookEnd = ref('')
const bookPurpose = ref('')
const bookHint = ref('')

const doQueryResources = async () => {
  tab3Busy.value = true
  pageError.value = ''
  try {
    const data = await invokeTool('office.resource.query', {
      ...(resourceType.value ? { type: resourceType.value } : {}),
      ...(resourceDate.value ? { date: resourceDate.value } : {}),
    })
    resources.value = ((data.result ?? {}) as { resources?: Resource[] }).resources ?? []
  } catch (e) {
    resources.value = []
    pageError.value = (e as Error).message || '资源查询失败'
  } finally {
    tab3Busy.value = false
  }
}

const doBook = async () => {
  if (!bookResourceId.value || !resourceDate.value || !bookStart.value || !bookEnd.value) {
    pageError.value = '预订需选定资源、日期与起止时间（HH:MM）'
    return
  }
  pageError.value = ''
  bookHint.value = ''
  try {
    const data = await invokeTool('office.resource.book', {
      resource_id: bookResourceId.value,
      date: resourceDate.value,
      start: bookStart.value,
      end: bookEnd.value,
      purpose: bookPurpose.value.trim(),
      idem_key: `web-${Date.now()}`,
    })
    const approvalId = (data as unknown as { approval_id?: string }).approval_id
    bookHint.value = approvalId
      ? `已提交审批（${approvalId}），批准后落台账；冲突预订会在批准执行期被拒`
      : '已提交，请到审批页处理'
    await doQueryResources()
  } catch (e) {
    pageError.value = (e as Error).message || '预订失败'
  }
}
</script>

<template>
  <div class="grid">
    <el-card shadow="never">
      <template #header>
        <div class="page-head">
          <span class="card-title">考勤与入离职</span>
        </div>
      </template>
      <span class="sub-title">考勤 / 加班时长</span>
      <div class="toolbar block-gap">
        <el-input v-model="person" placeholder="姓名" style="width: 140px" />
        <el-input v-model="month" placeholder="月份前缀如 2026-09" style="width: 180px" />
        <el-button type="primary" :loading="tab1Busy" @click="doAttendance">查询</el-button>
      </div>
      <div v-if="attendance" class="kpi-row">
        <el-statistic title="出勤记录数" :value="Object.values(attendance.status_count ?? {}).reduce((a, b) => a + b, 0)" />
        <el-statistic title="总工时" :value="attendance.total_hours ?? 0" />
        <el-statistic title="加班时长" :value="attendance.overtime_hours ?? 0" />
      </div>
      <p v-if="attendance?.overtime_rule" class="muted">{{ attendance.overtime_rule }}；加班申请草稿请走审批页 office.approval.draft。</p>
      <el-table v-if="attendance?.attendance?.length" :data="attendance.attendance" size="small" class="block-gap">
        <el-table-column prop="date" label="日期" width="130" />
        <el-table-column prop="status" label="状态" width="100" />
      </el-table>

      <el-divider />
      <span class="sub-title">入职指引 / 离职交接</span>
      <div class="toolbar block-gap">
        <el-select v-model="scene" style="width: 150px">
          <el-option label="入职指引" value="onboard" />
          <el-option label="离职交接" value="offboard" />
        </el-select>
        <el-input v-model="checklistPerson" placeholder="当事人姓名" style="width: 150px" />
        <el-button type="primary" :loading="tab2Busy" @click="doChecklist">一键生成</el-button>
      </div>
      <el-input
        v-if="scene === 'offboard'"
        v-model="handoverText"
        type="textarea"
        :rows="3"
        placeholder="交接事项，每行一条，格式“事项|责任人|截止”（责任人/截止可空）"
        class="block-gap"
      />
      <ul v-if="checklist?.sections?.length" class="plain-list">
        <li v-for="s in checklist.sections" :key="s">{{ s }}</li>
      </ul>
      <p v-for="r in checklist?.reminders ?? []" :key="r" class="muted">{{ r }}</p>
      <p v-if="checklist?.unfilled?.length" class="muted">待补充：{{ checklist.unfilled.join('；') }}</p>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="page-head">
          <span class="card-title">资源预订</span>
        </div>
      </template>
      <div class="toolbar">
        <el-select v-model="resourceType" placeholder="全部类型" clearable style="width: 150px">
          <el-option label="会议室" value="room" />
          <el-option label="工位" value="desk" />
          <el-option label="公务车辆" value="vehicle" />
        </el-select>
        <el-input v-model="resourceDate" placeholder="日期 YYYY-MM-DD" style="width: 170px" />
        <el-button :loading="tab3Busy" @click="doQueryResources">查询占用</el-button>
      </div>
      <el-table v-if="resources.length" :data="resources" size="small" class="block-gap">
        <el-table-column prop="name" label="资源" min-width="140" />
        <el-table-column prop="id" label="ID" width="110" />
        <el-table-column label="已订区间" min-width="200">
          <template #default="{ row }">
            <span class="muted">{{ (row.booked_slots ?? []).map((s: ResourceSlot) => `${s.date} ${s.start}-${s.end}`).join('；') || '空闲' }}</span>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else :image-size="80" description="先查询占用情况（空结果如实展示）" />

      <el-divider />
      <span class="sub-title">发起预订（写动作恒送审）</span>
      <div class="toolbar block-gap">
        <el-select v-model="bookResourceId" placeholder="选择资源" style="width: 170px">
          <el-option v-for="r in resources" :key="r.id" :label="`${r.name}（${r.id}）`" :value="r.id" />
        </el-select>
        <el-input v-model="bookStart" placeholder="起始 HH:MM" style="width: 130px" />
        <el-input v-model="bookEnd" placeholder="结束 HH:MM" style="width: 130px" />
      </div>
      <el-input v-model="bookPurpose" placeholder="用途说明" class="block-gap" />
      <el-button type="primary" @click="doBook">提交预订审批</el-button>
      <p v-if="bookHint" class="muted">{{ bookHint }}</p>
      <el-alert v-if="pageError" class="block-gap" type="error" :title="pageError" show-icon :closable="false" />
    </el-card>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  gap: 12px;
  margin-bottom: 12px;
}
.kpi-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 12px;
  margin: 14px 0 6px;
  padding: 12px 14px;
  background: #fafbfc;
  border: 1px solid var(--line);
}
.plain-list {
  margin: 8px 0;
  padding-left: 20px;
}
</style>
