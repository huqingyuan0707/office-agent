<script setup lang="ts">
// 职责：管理页（Element Plus 版）—— GET /admin/overview 聚合渲染（用户计数 / 任务与审批分状态 /
//       工具调用 Top / 最近裁决）；非 admin 403 → 壳红条如实提示权限不足，不降级给假数据
// 链路：router /admin → api.adminOverview → 分卡渲染：字段缺失不渲染不编造；看板只读聚合，
//       名单明细不出聚合口（要查单去审批/任务页）
// 对齐：AGENTS.md §4 前端红线（真实接口零 mock、失败置空 + 报错提示、var(--*) token + scoped）+
//       PRD §2.13 运营看板
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

const labelOf = (status: string) => (STATUS_LABEL as { [key: string]: string })[status] ?? status

const entriesOf = (m?: { [key: string]: number }) =>
  Object.entries(m ?? {}).map(([status, count]) => ({ status, count }))

// 分状态表的标签语义色：已通过与完成绿、驳回与失败红、待处理黄、其余蓝
const toneOf = (status: string) => {
  if (['approved', 'done'].includes(status)) return 'success'
  if (['rejected', 'failed'].includes(status)) return 'danger'
  if (['pending'].includes(status)) return 'warning'
  return 'primary'
}

const approvalTotal = () => Object.values(overview.value?.approvals ?? {}).reduce((a, b) => a + b, 0)

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
  <el-skeleton v-if="loading" :rows="8" animated />

  <template v-else-if="overview">
    <!-- 用户与审批总览：EP 统计卡 -->
    <el-card class="block-gap" shadow="never">
      <template #header>
        <div class="page-head"><span class="card-title">总览</span></div>
      </template>
      <div class="kpi-row">
        <el-statistic title="用户总数" :value="overview.users.total" />
        <el-statistic title="正常" :value="overview.users.active" />
        <el-statistic title="冻结" :value="overview.users.frozen" />
        <div class="kpi-link">
          <RouterLink to="/approvals" class="kpi-link-inner">
            <el-statistic title="审批单总数 · 去处理" :value="approvalTotal()" />
          </RouterLink>
        </div>
      </div>
    </el-card>

    <!-- 任务与审批：分状态计数（后端有什么状态就展示什么，不预设） -->
    <el-card class="block-gap" shadow="never">
      <template #header>
        <div class="page-head"><span class="card-title">任务与审批</span></div>
      </template>
      <el-row :gutter="16">
        <el-col :md="12" :span="24">
          <h3 class="sub-title">任务分状态</h3>
          <el-table :data="entriesOf(overview.tasks)" size="small" style="width: 100%">
            <el-table-column label="状态" min-width="120">
              <template #default="{ row }">
                <el-tag :type="toneOf(row.status)" size="small" effect="light" round>
                  {{ labelOf(row.status) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="count" label="数量" width="100" align="right" />
            <template #empty>
              <el-empty description="暂无任务数据" :image-size="60" />
            </template>
          </el-table>
        </el-col>
        <el-col :md="12" :span="24">
          <h3 class="sub-title">审批分状态</h3>
          <el-table :data="entriesOf(overview.approvals)" size="small" style="width: 100%">
            <el-table-column label="状态" min-width="120">
              <template #default="{ row }">
                <el-tag :type="toneOf(row.status)" size="small" effect="light" round>
                  {{ labelOf(row.status) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="count" label="数量" width="100" align="right" />
            <template #empty>
              <el-empty description="暂无审批数据" :image-size="60" />
            </template>
          </el-table>
        </el-col>
      </el-row>
    </el-card>

    <!-- 高频指令：工具调用 Top（Agent 使用率/功能访问的计数底座） -->
    <el-card class="block-gap" shadow="never">
      <template #header>
        <div class="page-head">
          <span class="card-title">高频工具</span>
          <el-tag size="small" type="info" round>
            调用总量 {{ overview.tool_calls.total }}
          </el-tag>
        </div>
      </template>
      <el-table :data="overview.tool_calls.top_tools" size="small" style="width: 100%">
        <el-table-column prop="name" label="工具" min-width="200">
          <template #default="{ row }">
            <span class="mono">{{ row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="count" label="调用次数" width="120" align="right" />
        <template #empty>
          <el-empty description="暂无调用记录" :image-size="60" />
        </template>
      </el-table>
    </el-card>

    <!-- 最近裁决：已决 5 单（翻页去审批页） -->
    <el-card shadow="never">
      <template #header>
        <div class="page-head">
          <span class="card-title">最近裁决</span>
          <RouterLink to="/approvals" class="more-link">查看全部 →</RouterLink>
        </div>
      </template>
      <el-table :data="overview.recent_decisions" size="small" style="width: 100%">
        <el-table-column prop="action" label="动作" min-width="180">
          <template #default="{ row }">
            <span class="mono">{{ row.action }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="applicant" label="申请人" width="130" />
        <el-table-column prop="approver" label="裁决人" width="130" />
        <el-table-column label="结论" width="110">
          <template #default="{ row }">
            <el-tag :type="toneOf(row.status)" size="small" effect="light" round>
              {{ labelOf(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="decided_at" label="时间" width="180">
          <template #default="{ row }">
            <span class="mono">{{ row.decided_at || '—' }}</span>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无已决审批单" :image-size="60" />
        </template>
      </el-table>
    </el-card>
  </template>

  <el-empty
    v-else
    :image-size="90"
    description="暂无运营数据（非管理员账号会收到权限不足提示）"
  />
</template>

<style scoped>
.kpi-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
}
.kpi-link {
  border-radius: var(--radius-sm);
  transition: background 0.15s ease;
}
.kpi-link:hover {
  background: var(--brand-soft);
}
.kpi-link-inner {
  display: block;
  color: inherit;
  text-decoration: none;
  padding: 0 8px;
}
.sub-title {
  margin-top: 0;
}
.more-link {
  margin-left: auto;
  font-size: 13px;
  color: var(--brand);
  text-decoration: none;
}
</style>