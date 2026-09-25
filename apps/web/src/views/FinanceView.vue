<script setup lang="ts">
// 职责：财务辅助页（PRD §2.10）—— 个人报销进度、部门费用统计与项目预算剩余额度，
//       走后端真实工具（finance.reimburse/expense、budget.query），零 mock
// 链路：router /finance → api.invokeTool；失败本页红条 + 结果置空，结果必带溯源展示
// 对齐：PRD §2.10 + AGENTS.md §4 前端红线（禁 mock 兜底）+ 后端红线（数值原值直出）
import { ref } from 'vue'
import { invokeTool } from '../api'

interface Reimburse {
  no: string
  title: string
  amount: number
  stage: string
  updated: string
}
interface Expense {
  total?: number
  count?: number
  by_category?: Record<string, number>
  budgets?: Record<string, { remaining?: number; usage_pct?: number | null }>
  skipped?: number
  source?: string
  fetched_at?: string
}
interface BudgetRow {
  project?: string
  budget?: number
  used?: number
  remaining?: number
  usage_pct?: number | null
  status?: string
}
interface Budget {
  rows?: BudgetRow[]
  summary?: { total_budget?: number; total_used?: number; total_remaining?: number }
  source?: string
  fetched_at?: string
}

// ---------- tab1：报销进度 ----------
const person = ref('')
const reimburses = ref<Reimburse[]>([])
const transit = ref(0)
const tab1Busy = ref(false)
const pageError = ref('')

const doReimburse = async () => {
  if (!person.value.trim()) return
  tab1Busy.value = true
  pageError.value = ''
  try {
    const data = await invokeTool('office.finance.reimburse', { person: person.value.trim() })
    const result = (data.result ?? {}) as { records?: Reimburse[]; transit_amount?: number }
    reimburses.value = result.records ?? []
    transit.value = result.transit_amount ?? 0
  } catch (e) {
    reimburses.value = []
    pageError.value = (e as Error).message || '报销查询失败'
  } finally {
    tab1Busy.value = false
  }
}

// ---------- tab2：部门费用 ----------
const department = ref('')
const month = ref('')
const expense = ref<Expense | null>(null)
const tab2Busy = ref(false)

const doExpense = async () => {
  if (!department.value.trim()) return
  tab2Busy.value = true
  pageError.value = ''
  try {
    const data = await invokeTool('office.finance.expense', {
      department: department.value.trim(),
      ...(month.value.trim() ? { month: month.value.trim() } : {}),
    })
    expense.value = (data.result ?? {}) as Expense
  } catch (e) {
    expense.value = null
    pageError.value = (e as Error).message || '费用统计失败'
  } finally {
    tab2Busy.value = false
  }
}

// ---------- tab3：预算额度 ----------
const project = ref('')
const budget = ref<Budget | null>(null)
const tab3Busy = ref(false)

const doBudget = async () => {
  if (!project.value.trim()) return
  tab3Busy.value = true
  pageError.value = ''
  try {
    const data = await invokeTool('office.budget.query', { project: project.value.trim() })
    budget.value = (data.result ?? {}) as Budget
  } catch (e) {
    budget.value = null
    pageError.value = (e as Error).message || '预算查询失败'
  } finally {
    tab3Busy.value = false
  }
}
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="page-head">
        <span class="card-title">财务辅助</span>
        <span class="head-hint">个人报销进度、部门费用台账与项目预算剩余额度查询（PRD §2.10）</span>
      </div>
    </template>
    <el-tabs>
      <el-tab-pane label="报销进度">
        <div class="toolbar">
          <el-input v-model="person" placeholder="报销人姓名" style="width: 180px" />
          <el-button type="primary" :loading="tab1Busy" @click="doReimburse">查询</el-button>
          <span v-if="reimburses.length" class="muted">在途金额（审批中+已通过未打款）：{{ transit }}</span>
        </div>
        <el-table v-if="reimburses.length" :data="reimburses" stripe size="small" style="width: 100%">
          <el-table-column prop="no" label="单号" width="140" />
          <el-table-column prop="title" label="事由" min-width="140" />
          <el-table-column prop="amount" label="金额" width="100" align="right" />
          <el-table-column prop="stage" label="当前节点" width="100" />
          <el-table-column prop="updated" label="更新" width="120" />
        </el-table>
        <el-empty v-else :image-size="80" description="输入报销人后查询（空结果如实展示）" />
      </el-tab-pane>

      <el-tab-pane label="部门费用台账">
        <div class="toolbar">
          <el-input v-model="department" placeholder="部门名" style="width: 160px" />
          <el-input v-model="month" placeholder="月份前缀如 2026-09" style="width: 180px" />
          <el-button type="primary" :loading="tab2Busy" @click="doExpense">简易统计</el-button>
        </div>
        <div v-if="expense" class="kpi-row">
          <el-statistic title="总额" :value="expense.total ?? 0" />
          <el-statistic title="笔数" :value="expense.count ?? 0" />
        </div>
        <el-table v-if="expense && Object.keys(expense.by_category ?? {}).length" :data="Object.entries(expense.by_category ?? {}).map(([k, v]) => ({ category: k, amount: v }))" size="small" class="block-gap">
          <el-table-column prop="category" label="类目" width="140" />
          <el-table-column prop="amount" label="金额" width="120" align="right" />
        </el-table>
        <p v-for="(b, name) in expense?.budgets ?? {}" :key="name" class="muted">
          {{ name }}：剩余额度 {{ b.remaining }}，使用率 {{ b.usage_pct ?? '—' }}%（预算联带）
        </p>
        <p v-if="expense?.source" class="muted">来源 {{ expense.source }} · {{ expense.fetched_at }}</p>
      </el-tab-pane>

      <el-tab-pane label="预算额度查询">
        <div class="toolbar">
          <el-input v-model="project" placeholder="输入项目名查询预算剩余额度" style="width: 280px" />
          <el-button type="primary" :loading="tab3Busy" @click="doBudget">查询</el-button>
        </div>
        <el-table v-if="budget?.rows?.length" :data="budget.rows" stripe size="small" style="width: 100%">
          <el-table-column prop="project" label="项目" min-width="140" />
          <el-table-column prop="budget" label="预算" width="110" align="right" />
          <el-table-column prop="used" label="已用" width="110" align="right" />
          <el-table-column prop="remaining" label="剩余" width="110" align="right" />
          <el-table-column prop="usage_pct" label="使用率%" width="100" align="right" />
          <el-table-column prop="status" label="状态" width="90" />
        </el-table>
        <p v-if="budget" class="muted">
          汇总剩余 {{ budget.summary?.total_remaining }} · 来源 {{ budget.source }} · {{ budget.fetched_at }}
        </p>
        <el-empty v-else :image-size="80" description="输入项目名后查询（结果带充足/紧张/超支标签与溯源）" />
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
.kpi-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 12px;
  margin: 14px 0 6px;
  padding: 12px 14px;
  background: #fafbfc;
  border: 1px solid var(--line);
}
</style>
