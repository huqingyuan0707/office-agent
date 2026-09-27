<script setup lang="ts">
// 职责：日程与提醒页（PRD §2.2 个人事务智能管理）——真实接线：待办办理/会议预约/空闲查询/工作台账
// 链路：router /schedule → 本页 → invokeTool office.todo.* / office.schedule.* / office.worklog.*
// 写动作恒送审：todo.update / todo.delete / schedule.create 由后端审批闸门把关，前端拿到 approval_id 如实提示
// 对齐：PRD §2.2 + AGENTS.md §4 前端红线（禁 mock 兜底，失败置空 + 报错提示）
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { invokeTool } from '../api'

interface TodoItem {
  id: string
  title: string
  status: string
  priority?: string
  due_date?: string
  owner?: string
}
interface FreeBusyPerson {
  person: string
  busy: { from: string; to: string; title?: string }[]
  free: { from: string; to: string }[]
}
interface WorklogPayload {
  period: string
  start_date: string
  end_date: string
  counts: { done: number; pending: number; schedules: number }
  worklog: string
}

const todos = ref<TodoItem[]>([])
const todoStatus = ref<string>('open')
const todoLoading = ref(false)
const todoError = ref('')

const busyForm = reactive({
  date: '',
  participants: [] as string[],
})
const freebusy = ref<FreeBusyPerson[]>([])
const busyLoading = ref(false)

const meetingForm = reactive({
  title: '',
  start: '',
  kind: 'meeting',
  duration_minutes: 60,
  attendees: [] as string[],
  location: '',
})
const meetingSubmitting = ref(false)

const worklog = ref<WorklogPayload | null>(null)
const worklogPeriod = ref<string>('weekly')
const worklogLoading = ref(false)

const reminderRules = [
  { type: '审批超时预警', rule: '复核员超过 24 小时未处理审批时告警' },
  { type: '任务到期提醒', rule: '待办临近 / 逾期前 N 小时站内推送' },
  { type: '会议临近通知', rule: '创建人 + 参会人逐人，会前 N 小时提醒' },
  { type: '项目节点预警', rule: '里程碑 N 天内倒计时提示' },
  { type: '定期简报', rule: '工作日上班推送今日待办与会话；周五提示生成周报草稿' },
]

const _today = () => new Date().toISOString().slice(0, 10)
const _fmtHour = (dt: string) => dt.replace('T', ' ').slice(0, 16)

const loadTodos = async () => {
  todoLoading.value = true
  todoError.value = ''
  try {
    const res = await invokeTool('office.todo.list', { status: todoStatus.value })
    if (!res || res.status !== 'ok') {
      throw new Error(res?.result !== undefined ? String(res.result) : '待办查询未返回结果')
    }
    const payload = res.result as { items?: TodoItem[] }
    todos.value = payload?.items ?? []
  } catch (e) {
    todoError.value = e instanceof Error ? e.message : String(e)
    todos.value = []
  } finally {
    todoLoading.value = false
  }
}

const markDone = async (row: TodoItem) => {
  try {
    const res = await invokeTool('office.todo.update', {
      todo_id: row.id,
      status: 'done',
    })
    if (res.requires_approval) {
      ElMessage.info(`已提交审批（单号 ${res.approval_id || '-'}），复核员批准后生效`)
    } else {
      ElMessage.success('待办已标记完成')
    }
    await loadTodos()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  }
}

const removeTodo = async (row: TodoItem) => {
  try {
    const res = await invokeTool('office.todo.delete', { todo_id: row.id })
    if (res.requires_approval) {
      ElMessage.info(`已提交审批（单号 ${res.approval_id || '-'}），复核员批准后生效`)
    } else {
      ElMessage.success('待办已删除')
    }
    await loadTodos()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  }
}

const queryFreeBusy = async () => {
  busyLoading.value = true
  try {
    const res = await invokeTool('office.schedule.freebusy', {
      date: busyForm.date || _today(),
      persons: busyForm.participants.length ? busyForm.participants : undefined,
    })
    if (!res || res.status !== 'ok') {
      throw new Error(res?.result !== undefined ? String(res.result) : '空闲查询未返回结果')
    }
    const payload = res.result as { persons?: FreeBusyPerson[]; work_hours?: string }
    freebusy.value = payload?.persons ?? []
    if (payload?.work_hours) {
      window.alert(`工作时段：${payload.work_hours}（空闲区间为工作时段减去当日会议忙碌段）`)
    }
  } catch (e) {
    freebusy.value = []
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    busyLoading.value = false
  }
}

const submitMeeting = async () => {
  if (!meetingForm.title.trim() || !meetingForm.start) {
    ElMessage.warning('请填写会议主题与开始时间')
    return
  }
  meetingSubmitting.value = true
  try {
    const res = await invokeTool('office.schedule.create', {
      title: meetingForm.title.trim(),
      kind: meetingForm.kind,
      start: meetingForm.start,
      duration_minutes: meetingForm.duration_minutes,
      attendees: meetingForm.attendees,
      location: meetingForm.location || undefined,
    })
    if (res.requires_approval) {
      ElMessage.info(`会议预约已提交审批（单号 ${res.approval_id || '-'}），复核员批准后落盘生效`)
    } else {
      ElMessage.success('会议预约已创建')
    }
    meetingForm.title = ''
    meetingForm.start = ''
    meetingForm.attendees = []
    meetingForm.location = ''
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    meetingSubmitting.value = false
  }
}

const loadWorklog = async () => {
  worklogLoading.value = true
  try {
    const res = await invokeTool('office.worklog.generate', { period: worklogPeriod.value })
    if (!res || res.status !== 'ok') {
      throw new Error(res?.result !== undefined ? String(res.result) : '工作台账生成未返回结果')
    }
    worklog.value = res.result as WorklogPayload
  } catch (e) {
    worklog.value = null
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    worklogLoading.value = false
  }
}

onMounted(() => {
  loadTodos()
  loadWorklog()
})
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="page-head">
        <span class="card-title">日程与提醒</span>
        <span class="head-hint">日程安排、会议预约、工作台账与主动提醒（PRD §2.2）</span>
      </div>
    </template>
    <el-tabs>
      <el-tab-pane label="我的待办">
        <div class="toolbar">
          <el-radio-group v-model="todoStatus" @change="loadTodos">
            <el-radio-button value="open">进行中</el-radio-button>
            <el-radio-button value="done">已完成</el-radio-button>
            <el-radio-button value="all">全部</el-radio-button>
          </el-radio-group>
          <el-button type="primary" :loading="todoLoading" @click="loadTodos">刷新</el-button>
        </div>
        <el-alert
          v-if="todoError"
          type="error"
          :title="todoError"
          show-icon
          :closable="false"
          style="margin-bottom: 12px"
        />
        <el-table v-loading="todoLoading" :data="todos" empty-text="暂无待办">
          <el-table-column prop="title" label="待办事项" min-width="220" />
          <el-table-column label="优先级" width="100">
            <template #default="{ row }">
              <el-tag :type="row.priority === 'high' ? 'danger' : row.priority === 'medium' ? 'warning' : 'info'">
                {{ row.priority || 'medium' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="due_date" label="截止" width="120" />
          <el-table-column label="操作" width="180">
            <template #default="{ row }">
              <el-button v-if="row.status !== 'done'" link type="primary" @click="markDone(row as TodoItem)">
                标记完成
              </el-button>
              <el-button link type="danger" @click="removeTodo(row as TodoItem)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="会议预约">
        <el-form label-width="88px" style="max-width: 560px">
          <el-form-item label="会议主题">
            <el-input v-model="meetingForm.title" placeholder="如：Q4 产品评审" />
          </el-form-item>
          <el-form-item label="类型">
            <el-radio-group v-model="meetingForm.kind">
              <el-radio-button value="meeting">会议</el-radio-button>
              <el-radio-button value="milestone">项目节点</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="开始时间">
            <el-date-picker
              v-model="meetingForm.start"
              type="datetime"
              :format="'YYYY-MM-DD HH:mm'"
              :value-format="'YYYY-MM-DD HH:mm'"
              placeholder="选择开始时间"
              style="width: 100%"
            />
          </el-form-item>
          <el-form-item label="时长(分钟)">
            <el-input-number v-model="meetingForm.duration_minutes" :min="0" :max="480" :step="15" />
          </el-form-item>
          <el-form-item label="参会人员">
            <el-select
              v-model="meetingForm.attendees"
              multiple
              filterable
              allow-create
              default-first-option
              placeholder="输入要搜索的用户名"
              style="width: 100%"
            />
          </el-form-item>
          <el-form-item label="地点">
            <el-input v-model="meetingForm.location" placeholder="如：3F 会议室 A" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="meetingSubmitting" @click="submitMeeting">
              发起预约并发送邀请
            </el-button>
          </el-form-item>
        </el-form>
        <el-divider content-position="left">人员空闲查询</el-divider>
        <div class="toolbar">
          <el-date-picker v-model="busyForm.date" type="date" :value-format="'YYYY-MM-DD'" placeholder="选择查询日期" style="width: 160px" />
          <el-select
            v-model="busyForm.participants"
            multiple
            filterable
            allow-create
            default-first-option
            placeholder="参会人用户名（缺省本人）"
            style="width: 220px"
          />
          <el-button type="primary" :loading="busyLoading" @click="queryFreeBusy">查询共同空闲</el-button>
        </div>
        <el-table v-if="freebusy.length" :data="freebusy" style="margin-top: 8px">
          <el-table-column prop="person" label="人员" width="140" />
          <el-table-column label="忙碌区间" min-width="220">
            <template #default="{ row }">
              <el-tag v-for="b in row.busy" :key="b.from + b.to" type="danger" style="margin: 2px">
                {{ b.from }}-{{ b.to }}{{ b.title ? ' ' + b.title : '' }}
              </el-tag>
              <span v-if="!row.busy.length" class="muted">无</span>
            </template>
          </el-table-column>
          <el-table-column label="空闲区间" min-width="220">
            <template #default="{ row }">
              <el-tag v-for="f in row.free" :key="f.from + f.to" type="success" style="margin: 2px">
                {{ f.from }}-{{ f.to }}
              </el-tag>
              <span v-if="!row.free.length" class="muted">无</span>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="工作台账">
        <div class="toolbar">
          <el-radio-group v-model="worklogPeriod" @change="loadWorklog">
            <el-radio-button value="daily">每日汇总</el-radio-button>
            <el-radio-button value="weekly">每周汇总</el-radio-button>
          </el-radio-group>
          <el-button type="primary" :loading="worklogLoading" @click="loadWorklog">生成个人工作台账</el-button>
        </div>
        <template v-if="worklog">
          <el-descriptions :column="3" border style="margin-bottom: 12px">
            <el-descriptions-item label="窗口">{{ worklog.start_date }} ~ {{ worklog.end_date }}</el-descriptions-item>
            <el-descriptions-item label="已完成待办">{{ worklog.counts.done }}</el-descriptions-item>
            <el-descriptions-item label="待完成">{{ worklog.counts.pending }}</el-descriptions-item>
          </el-descriptions>
          <pre class="worklog-body">{{ worklog.worklog }}</pre>
        </template>
        <el-empty v-else description="工作台账为空" />
      </el-tab-pane>

      <el-tab-pane label="智能提醒">
        <el-table :data="reminderRules">
          <el-table-column prop="type" label="提醒类型" width="160" />
          <el-table-column prop="rule" label="触发规则" />
        </el-table>
        <p class="muted">
          由后端通知扫描链按业务时区自动生成站内通知；覆盖下面五类，不在此页手动配置。
        </p>
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
  flex-wrap: wrap;
}
.worklog-body {
  margin: 0;
  padding: 12px 16px;
  background: var(--el-fill-color-light);
  border-radius: 6px;
  white-space: pre-wrap;
  line-height: 1.8;
  font-family: var(--el-font-family);
}
</style>