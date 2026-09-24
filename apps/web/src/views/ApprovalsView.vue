<script setup lang="ts">
// 职责：审批页 —— 审批表格（申请内容摘要 + EP 展开行看送审入参逐项）+ 批准/驳回操作
//       （驳回走 RejectDialog），操作后刷新列表
// 链路：router /approvals → api.listApprovals / api.decideApproval；驳回提交失败时弹窗不关、
//       输入不丢（错误经 props 传给弹窗就地显示）；列表失败 → 壳红条 + 列表置空
// 对齐：AGENTS.md §4 前端红线（真实接口零 mock、写动作状态如实回显、var(--*) token + scoped）；
//       治理口径：写动作恒送审的闸门价值取决于复核员看得见申请内容，故申请内容必须可见
import { inject, onMounted, ref } from 'vue'
import { decideApproval, listApprovals } from '../api'
import type { ApprovalItem } from '../api'
import RejectDialog from '../components/RejectDialog.vue'
import StatusBadge from '../components/StatusBadge.vue'

const shell = inject('shellError') as {
  showError: (msg: string) => unknown
  clearError: () => unknown
}

const approvals = ref<ApprovalItem[]>([])
const loading = ref(true)
const decidingId = ref('') // 正在处理的审批单（该行按钮禁用并显示进行中）
const rejectTarget = ref<ApprovalItem | null>(null) // 驳回弹窗目标单
const rejectError = ref('') // 弹窗内就地显示的提交错误

// ID 短显：32 位 hex 截前 8 位（完整值挂 tooltip 与展开区）
const shortId = (id: string) => (id.length > 10 ? `${id.slice(0, 8)}…` : id)

// 申请内容摘要：title 类语义字段优先、附参数计数（列宽有限，完整 JSON 在展开区；
// 摘要里绝不堆原始 JSON 长文，防止表格被撑爆出横向滚动、操作列出屏）
const SUMMARY_KEYS = ['title', 'name', 'goal', 'query', 'keyword', 'path']
const argsSummary = (args: unknown) => {
  if (typeof args !== 'object' || args === null || !Object.keys(args).length) return '无参数'
  const entries = Object.entries(args)
  const hit = SUMMARY_KEYS.flatMap((k) =>
    entries.filter(([name]) => name === k).map(([, value]) => value),
  )[0]
  const text = typeof hit === 'string' && hit ? hit : JSON.stringify(args)
  const brief = text.length > 40 ? `${text.slice(0, 40)}…` : text
  return `${brief}（${entries.length} 项）`
}

// 申请内容键值对：对象/数组按 JSON 展开、空值给占位符（如实展示入参，不做任何加工）
const argEntries = (args: unknown) => {
  if (typeof args !== 'object' || args === null) return []
  return Object.entries(args).map(([key, value]) => ({
    key,
    value:
      value === null || value === undefined
        ? '—'
        : typeof value === 'object'
          ? JSON.stringify(value)
          : String(value),
  }))
}

const loadApprovals = async () => {
  loading.value = true
  shell.clearError()
  try {
    approvals.value = await listApprovals()
  } catch (e) {
    approvals.value = []
    shell.showError((e as Error).message || '审批列表加载失败')
  } finally {
    loading.value = false
  }
}

// 审批决定唯一出口：成功才关弹窗；失败在弹窗内就地提示（非弹窗路径进壳红条），不丢输入
const submitDecision = async (id: string, action: 'approve' | 'reject', reason = '') => {
  shell.clearError()
  decidingId.value = id
  try {
    await decideApproval(id, action, reason)
    rejectTarget.value = null
    rejectError.value = ''
    await loadApprovals() // 审批决定后任务可能开始执行，刷新本表（任务页自行拉取）
  } catch (e) {
    const msg = (e as Error).message || '审批操作失败'
    if (rejectTarget.value?.id === id) rejectError.value = msg
    else shell.showError(msg)
  } finally {
    decidingId.value = ''
  }
}

// el-table 列插槽的 row 被 EP 推断为 DefaultRow，与后端返回项同构；此处收窄回领域类型
const onApprove = (row: unknown) => submitDecision((row as ApprovalItem).id, 'approve')

const onReject = (row: unknown) => {
  rejectTarget.value = row as ApprovalItem
  rejectError.value = ''
}

const closeReject = () => {
  rejectTarget.value = null
  rejectError.value = ''
}

const confirmReject = (reason: string) => {
  const target = rejectTarget.value
  if (!target) return
  submitDecision(target.id, 'reject', reason)
}

onMounted(loadApprovals)
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="page-head">
        <span class="card-title">审批列表</span>
        <el-tag v-if="approvals.length" size="small" type="info" round>
          {{ approvals.length }} 单
        </el-tag>
        <span class="muted head-hint">写动作恒送审：批准后系统以申请人的真实角色出站执行</span>
      </div>
    </template>

    <el-skeleton v-if="loading" :rows="6" animated />

    <el-table v-else :data="approvals" stripe style="width: 100%">
      <!-- 展开行：把送审入参逐项摊开（写动作恒送审的前提是复核员看得见内容） -->
      <el-table-column type="expand">
        <template #default="{ row }">
          <div class="detail-pane">
            <el-descriptions v-if="argEntries(row.args).length" :column="1" size="small" border>
              <el-descriptions-item
                v-for="entry in argEntries(row.args)"
                :key="entry.key"
                :label="entry.key"
              >
                {{ entry.value }}
              </el-descriptions-item>
            </el-descriptions>
            <el-text v-else type="info" size="small">该申请单未携带参数</el-text>
          </div>
        </template>
      </el-table-column>

      <el-table-column label="ID" width="110">
        <template #default="{ row }">
          <el-tooltip :content="row.id" placement="top">
            <span class="mono">{{ shortId(row.id) }}</span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column prop="action" label="审批类型" width="170" show-overflow-tooltip />
      <el-table-column label="申请内容" min-width="220">
        <template #default="{ row }">
          <el-tooltip :content="JSON.stringify(row.args ?? {})" placement="top">
            <span class="args-text">{{ argsSummary(row.args) }}</span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <StatusBadge :status="row.status" :label="row.status_label || row.status" />
        </template>
      </el-table-column>
      <el-table-column prop="applicant" label="申请人" width="120" show-overflow-tooltip />
      <el-table-column label="创建时间" width="175">
        <template #default="{ row }">
          <span class="muted time">{{ row.created_at }}</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <el-button
            size="small"
            type="primary"
            plain
            :disabled="decidingId === row.id"
            @click="onApprove(row)"
          >
            {{ decidingId === row.id ? '处理中…' : '批准' }}
          </el-button>
          <el-button size="small" type="danger" plain :disabled="decidingId === row.id" @click="onReject(row)">
            驳回
          </el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="暂无审批单" :image-size="80" />
      </template>
    </el-table>
  </el-card>

  <!-- 驳回理由弹窗（替代 window.prompt：可就地看错误、不丢已输入内容） -->
  <RejectDialog
    :target="rejectTarget"
    :error="rejectError"
    :busy="!!decidingId"
    @confirm="confirmReject"
    @cancel="closeReject"
  />
</template>

<style scoped>
.time {
  margin: 0;
}
.args-text {
  font-size: 13px;
}
/* 展开行：与主行视觉区隔 */
.detail-pane {
  padding: 6px 12px 10px 44px;
}
</style>