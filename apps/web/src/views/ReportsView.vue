<script setup lang="ts">
// 职责：报表页（Element Plus 版）—— 数据集四选一 → office.data.query 真查 → 表格（列取自首行键，
//       不预设 schema）+ 占比条形图（el-progress）+ office.data.analyze 统计卡（el-statistic）+
//       office.data.export markdown 预览与下载
// 链路：router /reports → api.invokeTool（query/analyze/export 三连）；查询失败 → 壳红条 + 置空，
//       分析/导出失败 → 本页红条（两路错误互不影响，失败绝不拿假数据顶）
// 对齐：AGENTS.md §4 前端红线（真实接口零 mock、禁直写 fetch、箭头函数、var(--*) token + scoped）+
//       PRD §5.3 数据可视化报表（复用 V1.1 数据工具链，图表用 EP 进度条不引新依赖）
import { inject, onMounted, ref } from 'vue'
import { DataAnalysis, Download, Refresh } from '@element-plus/icons-vue'
import { invokeTool } from '../api'

// 行形状以后端首行键为准（未知列不预设不编造；索引签名非 Record，风格约束见 skill）
interface DataRow {
  [key: string]: unknown
}

// 数据集 id 必须与 office.data.query 的 dataset 枚举逐字一致；numKey/catKey 只是默认展示列，
// 若后端行里没有该键则回退到首个数/文本列，缺失不编造
const DATASETS = [
  { id: 'sales', label: '业绩台账', numKey: 'amount', catKey: 'person' },
  { id: 'work', label: '工时台账', numKey: 'hours', catKey: 'person' },
  { id: 'project', label: '项目台账', numKey: 'progress', catKey: 'name' },
  { id: 'attendance', label: '考勤台账', numKey: '', catKey: 'person' },
] as const

const shell = inject('shellError') as {
  showError: (msg: string) => unknown
  clearError: () => unknown
}

const dataset = ref<string>('sales')
const rows = ref<DataRow[]>([])
const columns = ref<string[]>([])
const numKey = ref('amount')
const querying = ref(false)
const source = ref('') // 数据出处（后端 source + fetched_at，原样展示）

// ---------- 分析与导出 ----------
interface AnalyzeStats {
  count: number
  sum: number
  avg: number
  min: number
  max: number
  trend?: { direction: string; change_pct: string }
  anomalies?: { index: number; value: number }[]
  brief?: string
}
const stats = ref<AnalyzeStats | null>(null)
const analyzing = ref(false)
const exportText = ref('')
const exporting = ref(false)
const pageError = ref('') // 分析 / 导出失败红条（本页级，与壳红条分开）

const labelOf = (id: string) => DATASETS.find((d) => d.id === id)?.label ?? id
const catKeyOf = (id: string) => DATASETS.find((d) => d.id === id)?.catKey ?? ''

// 数值列：当前行里值为数字的列（图表与分析只吃真实数字列，无数字列则图表区如实空态）
const numericKeys = () => {
  if (!rows.value.length) return []
  return columns.value.filter((k) => typeof rows.value[0][k] === 'number')
}

const numValues = () =>
  rows.value.map((r) => r[numKey.value]).filter((v): v is number => typeof v === 'number')

const maxNum = () => {
  const vs = numValues()
  return vs.length ? Math.max(...vs) : 0
}

const barPct = (v: unknown) => {
  const max = maxNum()
  return typeof v === 'number' && max > 0 ? Math.round((v / max) * 100) : 0
}

const cellText = (v: unknown) => (v === null || v === undefined ? '' : String(v))

// 占比图数据：分类名 + 数值 + 占比（比例条按最大值归一，仅展示用）
const barRows = () =>
  rows.value.map((r) => ({
    category: cellText(r[catKeyOf(dataset.value)]),
    value: cellText(r[numKey.value]),
    pct: barPct(r[numKey.value]),
  }))

const doQuery = async () => {
  querying.value = true
  pageError.value = ''
  rows.value = []
  columns.value = []
  stats.value = null
  exportText.value = ''
  source.value = ''
  shell.clearError()
  try {
    const data = await invokeTool('office.data.query', { dataset: dataset.value, limit: 20 })
    const payload = (data.result ?? {}) as { rows?: DataRow[]; source?: string; fetched_at?: string }
    rows.value = payload.rows ?? []
    columns.value = rows.value.length ? Object.keys(rows.value[0]) : []
    // 默认数值列：预设键存在且为数字则用，否则回退首个数值列，否则留空（图表区空态）
    const preset = DATASETS.find((d) => d.id === dataset.value)?.numKey ?? ''
    const keys = numericKeys()
    numKey.value = keys.includes(preset) ? preset : (keys[0] ?? '')
    const src = payload.source ?? ''
    const at = payload.fetched_at ?? ''
    source.value = src ? `来源 ${src}${at ? ` · 取数时刻 ${at}` : ''}` : ''
  } catch (e) {
    rows.value = []
    shell.showError((e as Error).message || '报表数据加载失败')
  } finally {
    querying.value = false
  }
}

const doAnalyze = async () => {
  if (!numValues().length) return
  analyzing.value = true
  pageError.value = ''
  try {
    const data = await invokeTool('office.data.analyze', {
      label: `${labelOf(dataset.value)} · ${numKey.value}`,
      values: numValues(),
    })
    stats.value = (data.result ?? {}) as AnalyzeStats
  } catch (e) {
    stats.value = null
    pageError.value = (e as Error).message || '统计分析失败'
  } finally {
    analyzing.value = false
  }
}

const doExport = async () => {
  if (!rows.value.length) return
  exporting.value = true
  pageError.value = ''
  try {
    const data = await invokeTool('office.data.export', {
      title: labelOf(dataset.value),
      format: 'markdown',
      columns: columns.value,
      rows: rows.value,
    })
    exportText.value = ((data.result ?? {}) as { content?: string }).content ?? ''
  } catch (e) {
    exportText.value = ''
    pageError.value = (e as Error).message || '导出失败'
  } finally {
    exporting.value = false
  }
}

// 导出下载：把后端返回的真实文本存为本地 .md（客户端落盘，非 mock 数据）
const download = () => {
  if (!exportText.value) return
  const blob = new Blob([exportText.value], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${dataset.value}-report.md`
  a.click()
  URL.revokeObjectURL(url)
}

onMounted(doQuery)
</script>

<template>
  <div class="grid">
    <!-- 左区：数据集与数据表 -->
    <el-card shadow="never">
      <template #header>
        <div class="page-head">
          <span class="card-title">数据报表</span>
          <span v-if="source" class="muted head-hint">{{ source }}</span>
        </div>
      </template>

      <div class="toolbar">
        <el-select v-model="dataset" style="width: 180px" @change="doQuery">
          <el-option v-for="d in DATASETS" :key="d.id" :label="d.label" :value="d.id" />
        </el-select>
        <el-button type="primary" :icon="Refresh" :loading="querying" @click="doQuery">
          {{ querying ? '查询中…' : '刷新' }}
        </el-button>
      </div>

      <el-skeleton v-if="querying" :rows="5" animated />
      <el-table v-else-if="rows.length" :data="rows" stripe size="small" style="width: 100%">
        <el-table-column
          v-for="c in columns"
          :key="c"
          :prop="c"
          :label="c"
          min-width="120"
          show-overflow-tooltip
        >
          <template #default="{ row }">
            <span class="mono">{{ cellText(row[c]) }}</span>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else :image-size="80" description="暂无数据（空结果如实展示，不补假行）" />
    </el-card>

    <!-- 右区：占比图 + 统计 + 导出 -->
    <el-card shadow="never">
      <template #header>
        <div class="page-head">
          <span class="card-title">可视化</span>
        </div>
      </template>

      <el-select v-if="numericKeys().length" v-model="numKey" class="block-gap" style="width: 200px">
        <el-option v-for="k in numericKeys()" :key="k" :label="`数值列：${k}`" :value="k" />
      </el-select>

      <template v-if="rows.length && numKey && numValues().length">
        <el-table :data="barRows()" size="small" style="width: 100%">
          <el-table-column prop="category" label="分类" width="110" show-overflow-tooltip />
          <el-table-column label="占比（按最大值归一）" min-width="180">
            <template #default="{ row }">
              <el-progress :percentage="row.pct" :stroke-width="10" :show-text="false" />
            </template>
          </el-table-column>
          <el-table-column prop="value" label="数值" width="100" align="right">
            <template #default="{ row }">
              <span class="mono">{{ row.value }}</span>
            </template>
          </el-table-column>
        </el-table>
        <el-button class="block-gap analyze-btn" type="primary" :loading="analyzing" @click="doAnalyze">
          {{ analyzing ? '分析中…' : '统计分析' }}
        </el-button>
      </template>
      <el-empty
        v-else
        :image-size="80"
        description="当前无可图表数值列（考勤类文本台账如实展示表格即可）"
      />

      <div v-if="stats" class="kpi-row">
        <el-statistic title="样本数" :value="stats.count" />
        <el-statistic title="合计" :value="stats.sum" />
        <el-statistic title="均值" :value="stats.avg" :precision="2" />
        <el-statistic title="最大 / 最小" :value="stats.max">
          <template #suffix>
            <span class="kpi-suffix">/ {{ stats.min }}</span>
          </template>
        </el-statistic>
      </div>
      <div v-if="stats" class="trend-row">
        <span class="sub-title">趋势</span>
        <el-tag
          size="small"
          :type="stats.trend?.direction === '下降' ? 'danger' : 'success'"
          effect="light"
          round
        >
          {{ stats.trend?.direction ?? '—' }}
          <template v-if="stats.trend?.change_pct">（{{ stats.trend.change_pct }}）</template>
        </el-tag>
        <el-tag v-if="stats.anomalies?.length" size="small" type="warning" effect="light" round>
          异动 {{ stats.anomalies.length }} 处
        </el-tag>
      </div>
      <p v-if="stats?.brief" class="muted">{{ stats.brief }}</p>

      <div v-if="rows.length" class="export-row">
        <el-button :loading="exporting" @click="doExport">
          {{ exporting ? '导出中…' : '预览 markdown 导出' }}
        </el-button>
        <el-button v-if="exportText" :icon="Download" @click="download">下载 .md</el-button>
      </div>
      <pre v-if="exportText" class="code-block">{{ exportText }}</pre>

      <el-alert
        v-if="pageError"
        class="block-gap"
        type="error"
        :title="pageError"
        show-icon
        :closable="false"
      />
    </el-card>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  gap: 10px;
  margin-bottom: 14px;
}
.analyze-btn {
  margin-top: 14px;
}
/* 统计卡：四格等宽，ep statistic 自带标题/数值层级 */
.kpi-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 12px;
  margin: 14px 0 6px;
  padding: 12px 14px;
  background: #fafbfc;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
}
.kpi-suffix {
  font-size: 13px;
  color: var(--muted);
}
.trend-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}
.export-row {
  display: flex;
  gap: 10px;
  margin-top: 14px;
}
</style>