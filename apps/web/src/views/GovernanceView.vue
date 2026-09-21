<script setup lang="ts">
// 职责：治理页 —— GET /governance/status 分卡渲染（总览计数 / 上游提供方含缺失态 / 审计与指标）
// 链路：router /governance → api.governanceStatus → 如实渲染：任何字段缺失不编造、不造兜底数据
// 对齐：AGENTS.md §4 前端红线（真实接口零 mock、失败置空 + 报错提示）
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

const metricEntries = () => Object.entries(status.value?.metrics?.counters ?? {})

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
  <p v-if="loading" class="empty">加载中…</p>

  <template v-else-if="status">
    <!-- 总览：应用信息 + 工具/审批/审计计数 -->
    <section class="card block-gap">
      <h2>总览</h2>
      <p v-if="status.app || status.version || status.env" class="muted">
        {{ status.app }}<template v-if="status.version"> · v{{ status.version }}</template
        ><template v-if="status.env"> · 环境 {{ status.env }}</template>
      </p>
      <div class="stat-grid">
        <div v-if="status.tools?.total !== undefined" class="stat">
          <div class="stat-value">{{ status.tools.total }}</div>
          <div class="stat-label">
            已注册工具<template v-if="status.tools.remote !== undefined">
              （远程 {{ status.tools.remote }} · 本地 {{ status.tools.local }}）</template
            >
          </div>
        </div>
        <RouterLink v-if="status.pending_approvals !== undefined" to="/approvals" class="stat stat-link">
          <div class="stat-value">{{ status.pending_approvals }}</div>
          <div class="stat-label">待审批 · 去处理 →</div>
        </RouterLink>
        <div v-if="status.audit_recent !== undefined" class="stat">
          <div class="stat-value">{{ status.audit_recent }}</div>
          <div class="stat-label">近期审计条目</div>
        </div>
      </div>
    </section>

    <!-- 上游提供方：已配置表 + 被引用但缺失的提醒 -->
    <section class="card block-gap">
      <h2>上游提供方</h2>
      <div v-if="status.providers?.length" class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>提供方</th>
              <th>地址</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in status.providers" :key="p.provider_id">
              <td class="mono">{{ p.provider_id }}</td>
              <td class="mono">{{ p.endpoint }}</td>
              <td><span class="badge ok">已配置</span></td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else class="empty">暂无已配置的提供方</p>
      <div v-if="status.providers_missing?.length" class="missing-box">
        <p class="muted">以下提供方被工具引用但尚未配置（对应工具调用必失败）：</p>
        <span v-for="id in status.providers_missing" :key="id" class="badge warn">
          {{ id }} · 缺配置
        </span>
      </div>
    </section>

    <!-- 可观测指标：成功率 + 计数器表 -->
    <section class="card">
      <h2>可观测指标</h2>
      <template v-if="status.metrics">
        <p v-if="status.metrics.enabled !== undefined || status.metrics.dir" class="muted">
          <template v-if="status.metrics.enabled !== undefined">
            观测{{ status.metrics.enabled ? '已开启' : '未开启' }}</template
          ><template v-if="status.metrics.dir">
            · 落盘目录 <code class="mono">{{ status.metrics.dir }}</code></template
          >
        </p>
        <div class="stat-grid">
          <div v-for="r in rateStats" :key="r.key" class="stat">
            <div class="stat-value" :class="{ muted: r.value == null }">
              {{ r.value == null ? '—' : `${(r.value * 100).toFixed(1)}%` }}
            </div>
            <div class="stat-label">{{ r.label }}{{ r.value == null ? '（样本不足）' : '' }}</div>
          </div>
        </div>
        <div v-if="metricEntries().length" class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>计数器</th>
                <th>次数</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="[k, v] in metricEntries()" :key="k">
                <td class="mono">{{ k }}</td>
                <td>{{ v }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
      <p v-else class="empty">暂无指标数据</p>
    </section>
  </template>

  <p v-else class="empty">暂无治理状态数据</p>
</template>
