<script setup lang="ts">
// 职责：审批页 —— 审批表格（含申请内容摘要 + 详情展开）+ 批准/驳回操作（驳回走 RejectDialog），
//       操作后刷新列表
// 链路：router /approvals → api.listApprovals / api.decideApproval；驳回提交失败时弹窗不关、
//       输入不丢（错误经 props 传给弹窗就地显示）；列表失败 → 壳红条 + 列表置空
// 对齐：AGENTS.md §4 前端红线（真实接口零 mock、写动作状态如实回显）；
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
const expandedId = ref('') // 展开申请内容详情的那一行（同一时刻只展开一条）

// 申请内容摘要：超长截断，完整 JSON 挂 title（复核员一眼看出要批什么）
const argsBrief = (args: unknown) => {
  const text = JSON.stringify(args ?? {})
  return text.length > 60 ? `${text.slice(0, 60)}…` : text
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

const toggleDetail = (id: string) => {
  expandedId.value = expandedId.value === id ? '' : id
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

const onApprove = (item: ApprovalItem) => submitDecision(item.id, 'approve')

const onReject = (item: ApprovalItem) => {
  rejectTarget.value = item
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
  <section class="card">
    <h2>
      审批列表
      <span v-if="approvals.length" class="badge">{{ approvals.length }}</span>
    </h2>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>审批类型</th>
            <th>目标</th>
            <th>申请内容</th>
            <th>状态</th>
            <th>申请人</th>
            <th>创建时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading">
            <td colspan="8" class="empty">加载中…</td>
          </tr>
          <tr v-else-if="!approvals.length">
            <td colspan="8" class="empty">暂无审批单</td>
          </tr>
          <template v-else>
            <template v-for="a in approvals" :key="a.id">
              <tr>
                <td class="mono">{{ a.id }}</td>
                <td>{{ a.action }}</td>
                <td>{{ a.target }}</td>
                <td class="args-cell">
                  <span class="mono muted args-brief" :title="JSON.stringify(a.args ?? {})">
                    {{ argsBrief(a.args) }}
                  </span>
                  <button class="btn small" @click="toggleDetail(a.id)">
                    {{ expandedId === a.id ? '收起' : '详情' }}
                  </button>
                </td>
                <td>
                  <StatusBadge :status="a.status" :label="a.status_label || a.status" />
                </td>
                <td>{{ a.applicant }}</td>
                <td class="muted">{{ a.created_at }}</td>
                <td class="ops">
                  <button class="btn small" :disabled="decidingId === a.id" @click="onApprove(a)">
                    {{ decidingId === a.id ? '处理中…' : '批准' }}
                  </button>
                  <button
                    class="btn small danger"
                    :disabled="decidingId === a.id"
                    @click="onReject(a)"
                  >
                    驳回
                  </button>
                </td>
              </tr>
              <!-- 详情行：把送审入参逐项摊开（写动作恒送审的前提是复核员看得见内容） -->
              <tr v-if="expandedId === a.id" class="detail-row">
                <td colspan="8">
                  <dl v-if="argEntries(a.args).length" class="args-detail">
                    <template v-for="entry in argEntries(a.args)" :key="entry.key">
                      <dt>{{ entry.key }}</dt>
                      <dd>{{ entry.value }}</dd>
                    </template>
                  </dl>
                  <p v-else class="muted">该申请单未携带参数</p>
                </td>
              </tr>
            </template>
          </template>
        </tbody>
      </table>
    </div>
  </section>

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
.args-cell {
  max-width: 320px;
}
.args-cell .btn {
  margin-top: 4px;
}
/* 详情行：与主行视觉区隔，入参逐项左键右值 */
.detail-row td {
  background: var(--bg);
  border-top: 1px dashed var(--line-strong);
}
.args-detail {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 4px 12px;
  margin: 0;
}
.args-detail dt {
  color: var(--sub);
  font-size: 12px;
}
.args-detail dd {
  margin: 0;
  font-size: 13px;
  word-break: break-all;
}
</style>
