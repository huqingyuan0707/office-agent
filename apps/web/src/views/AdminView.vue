<script setup lang="ts">
// 职责：管理页 —— GET /admin/overview 聚合渲染（用户计数 / 任务与审批分状态 / 工具调用 Top /
//       最近裁决）；非 admin 403 → 壳红条如实提示权限不足，不降级给假数据
// 链路：router /admin → api.adminOverview → 分卡渲染：字段缺失不渲染不编造；看板只读聚合，
//       名单明细不出聚合口（要查单去审批/任务页）
// 对齐：AGENTS.md §4 前端红线（真实接口零 mock、失败置空 + 报错提示）+ PRD §2.13 运营看板
import { inject, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { adminOverview } from '../api'
import type { AdminOverview } from '../api'

const shell = inject('shellError') as {
  showError: (msg: string) => unknown
  clearError: () => unknown
}

// 状态中文化映射（后端状态原值展示兜底：未知状态显示原值不丢信息）
const STATUS_LABEL = {
  pending: '待处理',
  running: '运行中',
  done: '已完成',
  failed: '失败',
  approved: '已通过',
  rejected: '已驳回',
} as const

const overview = ref<AdminOverview | null>(null)
const loading = ref(true)

const labelOf = (status: string) =>
  (STATUS_LABEL as { [key: string]: string })[status] ?? status

const entriesOf = (m?: { [key: string]: number }) => Object.entries(m ?? {})

const loadOverview = async () => {
  loading.value = true
  overview.value = null
  shell.clearError()
  try {
    overview.value = await adminOverview()
  } catch (e) {
    overview.value = null
    shell.showError((e as Error).message || '运营数据加载失败')
  } finally {
    loading.value = false
  }
}

onMounted(loadOverview)
</script>

<template>
  <p v-if="loading" class="empty">加载中…</p>

  <template v-else-if="overview">
    <!-- 用户：总数/正常/冻结 -->
    <section class="card block-gap">
      <h2>用户</h2>
      <div class="stat-grid">
        <div class="stat">
          <div class="stat-value">{{ overview.users.total }}</div>
          <div class="stat-label">用户总数</div>
        </div>
        <div class="stat">
          <div class="stat-value">{{ overview.users.active }}</div>
          <div class="stat-label">正常</div>
        </div>
        <div class="stat">
          <div class="stat-value">{{ overview.users.frozen }}</div>
          <div class="stat-label">冻结</div>
        </div>
        <RouterLink to="/approvals" class="stat stat-link">
          <div class="stat-value">{{ Object.values(overview.approvals).reduce((a, b) => a + b, 0) }}</div>
          <div class="stat-label">审批单总数 · 去处理 →</div>
        </RouterLink>
      </div>
    </section>

    <!-- 任务与审批：分状态计数（后端有uspension什么状态就展示什么，不预设） -->
    <section class="card block-gap">
      <h2>任务与审批</h2>
      <div class="grid">
        <div>
          <h3 class="muted">任务分状态</h3>
          <div v-if="entriesOf(overview.tasks).length" class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>状态</th>
                  <th>数量</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="[k, v] in entriesOf(overview.tasks)" :key="k">
                  <td>{{ labelOf(k) }}</td>
                  <td>{{ v }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-else class="empty">暂无任务数据</p>
        </div>
        <div>
          <h3 class="muted">审批分状态</h3>
          <div v-if="entriesOf(overview.approvals).length" class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>状态</th>
                  <th>数量</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="[k, v] in entriesOf(overview.approvals)" :key="k">
                  <td>{{ labelOf(k) }}</td>
                  <td>{{ v }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-else class="empty">暂无审批数据</p>
        </div>
      </div>
    </section>

    <!-- 高频指令：工具调用 Top（Agent 使用率/功能访问的计数底座） -->
    <section class="card block-gap">
      <h2>高频工具</h2>
      <p class="muted">工具调用总量 {{ overview.tool_calls.total }}</p>
      <div v-if="overview.tool_calls.top_tools.length" class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>工具</th>
              <th>调用次数</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="t in overview.tool_calls.top_tools" :key="t.name">
              <td class="mono">{{ t.name }}</td>
              <td>{{ t.count }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else class="empty">暂无调用记录</p>
    </section>

    <!-- 最近裁决：已决 5 单（翻页去审批页） -->
    <section class="card">
      <h2>最近裁决</h2>
      <div v-if="overview.recent_decisions.length" class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>动作</th>
              <th>申请人</th>
              <th>裁决人</th>
              <th>结论</th>
              <th>时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="d in overview.recent_decisions" :key="d.id">
              <td class="mono">{{ d.action }}</td>
              <td>{{ d.applicant }}</td>
              <td>{{ d.approver }}</td>
              <td>{{ labelOf(d.status) }}</td>
              <td class="mono">{{ d.decided_at || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else class="empty">暂无已决审批单</p>
    </section>
  </template>

  <p v-else class="empty">暂无运营数据（非管理员账号会收到权限不足提示）</p>
</template>
