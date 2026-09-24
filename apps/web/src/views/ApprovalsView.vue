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

// ID 短显：32 位 hex 截前 8 位（完整值挂 title，详情展开区也有）
const shortId = (id: string) => (id.length > 10 ? `${id.slice(0, 8)}…` : id)

// 申请内容摘要：title 类语义字段优先、附参数计数（列宽有限，完整 JSON 在详情展开区；
// 摘要里绝不堆原始 JSON 长文，防止表格被撑爆出横向滚动、操作列出屏）
const SUMMARY_KEYS = ['title', 'name', 'goal', 'query', 'keyword', 'path']
const argsSummary = (args: unknown) => {
  if (typeof args !== 'object' || args === null || !Object.keys(args).length) return '无参数'
  const entries = Object.entries(args)
  const hit = SUMMARY_KEYS.flatMap((k) =>
    entries.filter(([name]) => name === k).map(([, value]) => value),
  )[0]
  const text = typeof hit === 'string' && hit ? hit : JSON.stringify(args)
  const brief = text.length > 60 ? `${text.slice(0, 60)}…` : text
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
        <colgroup>
          <col style="width: 96px" />
          <col style="width: 158px" />
          <col />
          <col style="width: 84px" />
          <col style="width: 88px" />
          <col style="width: 152px" />
          <col style="width: 140px" />
        </colgroup>
        <thead>
          <tr>
            <th>ID</th>
            <th>审批类型</th>
            <th>申请内容</th>
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
            <template v-for="a in approvals" :key="a.id">
              <tr>
                <td class="mono id-cell" :title="a.id">{{ shortId(a.id) }}</td>
                <td>{{ a.action }}</td>
                <td class="args-cell">
                  <div class="args-line">
                    <span class="args-text" :title="JSON.stringify(a.args ?? {})">
                      {{ argsSummary(a.args) }}
                    </span>
                    <button class="btn small" @click="toggleDetail(a.id)">
                      {{ expandedId === a.id ? '收起' : '详情' }}
                    </button>
                  </div>
                </td>
                <td>
                  <StatusBadge :status="a.status" :label="a.status_label || a.status" />
                </td>
                <td>{{ a.applicant }}</td>
                <td class="muted time-cell">{{ a.created_at }}</td>
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
                <td colspan="7">
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
/* fixed 布局：列宽由 colgroup 钉死，内容再长也不撑爆表格（全局 nowrap 会导致
   申请内容长文把操作列挤出屏幕，本页按列覆盖） */
table {
  table-layout: fixed;
}
/* 溢出兜底：fixed 列宽被窄视口压缩时内容一律裁剪省略，
   绝不画出单元格叠压到邻列（全局 nowrap 会溢出画出，这里收敛） */
th,
td {
  overflow: hidden;
  text-overflow: ellipsis;
}
.id-cell {
  overflow: hidden;
  text-overflow: ellipsis;
}
/* 申请内容列：允许换行 + 两行截断，完整 JSON 挂 title、全量在详情展开区 */
.args-cell {
  white-space: normal;
  word-break: break-all;
}
.args-line {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}
.args-line .btn {
  flex: none;
}
.args-text {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  font-size: 13px;
  min-width: 0;
}
/* 时间与操作列保持单行不换（fixed 下 nowrap 内容溢出需裁剪防顶开邻列） */
.time-cell {
  overflow: hidden;
  text-overflow: ellipsis;
}
.ops {
  white-space: nowrap;
}
/* 详情行：与主行视觉区隔，入参逐项左键右值 */
.detail-row td {
  background: var(--bg);
  border-top: 1px dashed var(--line-strong);
  white-space: normal;
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
