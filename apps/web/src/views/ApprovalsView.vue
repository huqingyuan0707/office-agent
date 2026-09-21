<script setup lang="ts">
// 职责：审批页 —— 审批表格 + 批准/驳回操作（驳回走 RejectDialog），操作后刷新列表
// 链路：router /approvals → api.listApprovals / api.decideApproval；驳回提交失败时弹窗不关、
//       输入不丢（错误经 props 传给弹窗就地显示）；列表失败 → 壳红条 + 列表置空
// 对齐：AGENTS.md §4 前端红线（真实接口零 mock、写动作状态如实回显）
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
            <th>状态</th>
            <th>申请人</th>
            <th>创建时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading">
            <td colspan="7" class="empty">加载中…</td>
          </tr>
          <tr v-else-if="!approvals.length">
            <td colspan="7" class="empty">暂无审批单</td>
          </tr>
          <template v-else>
            <tr v-for="a in approvals" :key="a.id">
              <td class="mono">{{ a.id }}</td>
              <td>{{ a.action }}</td>
              <td>{{ a.target }}</td>
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
