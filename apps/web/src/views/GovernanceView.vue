<script setup lang="ts">
// 职责：治理页（Element Plus 版）—— GET /governance/status 分卡渲染（总览计数 / 上游提供方含
//       缺失态 / 审计与指标）
// 链路：router /governance → api.governanceStatus → 如实渲染：任何字段缺失不编造、不造兜底数据
// 对齐：AGENTS.md §4 前端红线（真实接口零 mock、失败置空 + 报错提示、var(--*) token + scoped）
import { computed, inject, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { governanceStatus } from '../api'
import type { GovernanceStatus } from '../api'

const shell = inject('shellError') as {
  showError: (msg: string) => unknown
  clearError: () => unknown
}

const status = ref<GovernanceStatus | null>(null)
const loading = ref(true)

// 可观测指标三项成功率（后端口径：null 表示样本不足，前端显示「—」而非假装 0）
const rateStats = computed(() => {
  const m = status.value?.metrics
  return [
    { key: 'llm', label: 'LLM 成功率', value: m?.llm_ok_rate },
    { key: 'tool', label: '工具成功率', value: m?.tool_ok_rate },
    { key: 'linkage', label: '联动成功率', value: m?.linkage_ok_rate },
  ]
})

const metricEntries = () =>
  Object.entries(status.value?.metrics?.counters ?? {}).map(([name, count]) => ({ name, count }))

const loadStatus = async () => {
  loading.value = true
  status.value = null
  shell.clearError()
  try {
    status.value = await governanceStatus()
  } catch (e) {
    status.value = null
    shell.showError((e as Error).message || '治理状态加载失败')
  } finally {
    loading.value = false
  }
}

onMounted(loadStatus)
</script>

<template>
  <el-skeleton v-if="loading" :rows="8" animated />

  <template v-else-if="status">
    <!-- 总览：应用信息 + 工具/审批/审计计数 -->
    <el-card class="block-gap" shadow="never">
      <template #header>
        <div class="page-head">
          <span class="card-title">总览</span>
          <span v-if="status.app || status.version || status.env" class="muted head-hint">
            {{ status.app }}<template v-if="status.version"> · v{{ status.version }}</template
            ><template v-if="status.env"> · 环境 {{ status.env }}</template>
          </span>
        </div>
      </template>

      <div class="kpi-row">
        <el-statistic v-if="status.tools?.total !== undefined" title="已注册工具" :value="status.tools.total">
          <template #suffix>
            <span class="kpi-suffix">远程 {{ status.tools.remote }} · 本地 {{ status.tools.local }}</span>
          </template>
        </el-statistic>
        <div v-if="status.pending_approvals !== undefined" class="kpi-link">
          <RouterLink to="/approvals" class="kpi-link-inner">
            <el-statistic title="待审批 · 去处理" :value="status.pending_approvals" />
          </RouterLink>
        </div>
        <el-statistic
          v-if="status.audit_recent !== undefined"
          title="近期审计条目"
          :value="status.audit_recent"
        />
      </div>
    </el-card>

    <!-- 上游提供方：已配置表 + 被引用但缺失的提醒 -->
    <el-card class="block-gap" shadow="never">
      <template #header>
        <div class="page-head"><span class="card-title">上游提供方</span></div>
      </template>

      <el-table v-if="status.providers?.length" :data="status.providers" size="small" style="width: 100%">
        <el-table-column prop="provider_id" label="提供方" min-width="160">
          <template #default="{ row }">
            <span class="mono">{{ row.provider_id }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="endpoint" label="地址" min-width="240">
          <template #default="{ row }">
            <span class="mono">{{ row.endpoint }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default>
            <el-tag size="small" type="success" effect="light" round>已配置</el-tag>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else :image-size="70" description="暂无已配置的提供方" />

      <el-alert
        v-if="status.providers_missing?.length"
        class="block-gap"
        type="warning"
        show-icon
        :closable="false"
        title="以下提供方被工具引用但尚未配置（对应工具调用必失败）"
      >
        <div class="missing-tags">
          <el-tag v-for="id in status.providers_missing" :key="id" size="small" type="warning" effect="light">
            {{ id }} · 缺配置
          </el-tag>
        </div>
      </el-alert>
    </el-card>

    <!-- 可观测指标：成功率 + 计数器表 -->
    <el-card shadow="never">
      <template #header>
        <div class="page-head">
          <span class="card-title">可观测指标</span>
          <span v-if="status.metrics" class="muted head-hint">
            <template v-if="status.metrics.enabled !== undefined">
              观测{{ status.metrics.enabled ? '已开启' : '未开启' }}
            </template>
            <template v-if="status.metrics.dir">
              · 落盘目录 <span class="mono">{{ status.metrics.dir }}</span>
            </template>
          </span>
        </div>
      </template>

      <template v-if="status.metrics">
        <div class="rate-grid">
          <div v-for="r in rateStats" :key="r.key" class="rate-tile">
            <div class="rate-value" :class="{ nil: r.value == null }">
              {{ r.value == null ? '—' : `${(r.value * 100).toFixed(1)}%` }}
            </div>
            <el-progress
              v-if="r.value != null"
              :percentage="Math.round(r.value * 1000) / 10"
              :stroke-width="6"
              :show-text="false"
            />
            <div class="rate-label">
              {{ r.label }}{{ r.value == null ? '（样本不足）' : '' }}
            </div>
          </div>
        </div>

        <el-table v-if="metricEntries().length" :data="metricEntries()" size="small" style="width: 100%">
          <el-table-column prop="name" label="计数器" min-width="220">
            <template #default="{ row }">
              <span class="mono">{{ row.name }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="count" label="次数" width="120" align="right" />
        </el-table>
      </template>
      <el-empty v-else :image-size="70" description="暂无指标数据" />
    </el-card>
  </template>

  <el-empty v-else :image-size="90" description="暂无治理状态数据" />
</template>

<style scoped>
.kpi-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px;
}
.kpi-suffix {
  font-size: 12px;
  color: var(--muted);
  margin-left: 6px;
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
.missing-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}
.rate-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 14px;
  margin-bottom: 14px;
}
.rate-tile {
  padding: 12px 14px;
  background: #fafbfc;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
}
.rate-value {
  font-size: 22px;
  font-weight: 700;
  line-height: 1.2;
  margin-bottom: 6px;
}
.rate-value.nil {
  color: var(--muted);
  font-weight: 500;
}
.rate-label {
  font-size: 12px;
  color: var(--muted);
  margin-top: 6px;
}
</style>