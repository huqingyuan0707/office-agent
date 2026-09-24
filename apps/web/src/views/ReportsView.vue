<script setup lang="ts">
// 职责：报表页 —— 数据集四选一 → office.data.query 真查 → 表格（列取自首行键，不预设 schema）+
//       数值列 CSS 条形图 + office.data.analyze 统计卡 + office.data.export markdown 预览与下载
// 链路：router /reports → api.invokeTool（query/analyze/export 三连）；查询失败 → 壳红条 + 置空，
//       分析/导出失败 → 本页红条（两路错误互不影响，失败绝不拿假数据顶）
// 对齐：AGENTS.md §4 前端红线（真实接口零 mock、禁直写 fetch、箭头函数、var(--*) token）+
//       PRD §5.3 数据可视化报表（复用 V1.1 数据工具链，图表为 CSS 条形不引新依赖）
import { inject, onMounted, ref } from 'vue'
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
    source.value = src ? `来源 ${src}` : ''
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
    <section class="card">
      <h2>数据报表</h2>
      <label class="field">
        <span>数据集</span>
        <select v-model="dataset" @change="doQuery">
          <option v-for="d in DATASETS" :key="d.id" :value="d.id">{{ d.label }}</option>
        </select>
      </label>
      <button class="btn primary" :disabled="querying" @click="doQuery">
        {{ querying ? '查询中…' : '刷新' }}
      </button>
      <p v-if="source" class="muted">{{ source }}</p>

      <div v-if="rows.length" class="table-wrap">
        <table>
          <thead>
            <tr>
              <th v-for="c in columns" :key="c">{{ c }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(r, i) in rows" :key="i">
              <td v-for="c in columns" :key="c" class="mono">{{ cellText(r[c]) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else class="empty">暂无数据（空结果如实展示，不补假行）</p>
    </section>

    <!-- 右区：条形图 + 统计 + 导出 -->
    <section class="card">
      <h2>可视化</h2>
      <label v-if="numericKeys().length" class="field">
        <span>数值列</span>
        <select v-model="numKey">
          <option v-for="k in numericKeys()" :key="k" :value="k">{{ k }}</option>
        </select>
      </label>

      <template v-if="rows.length && numKey && numValues().length">
        <div class="bars">
          <div v-for="(r, i) in rows" :key="i" class="bar-row">
            <span class="bar-cat">{{ cellText(r[DATASETS.find((d) => d.id === dataset)?.catKey ?? '']) }}</span>
            <div class="bar-track">
              <div class="bar-fill" :style="{ width: barPct(r[numKey]) + '%' }"></div>
            </div>
            <span class="bar-val mono">{{ cellText(r[numKey]) }}</span>
          </div>
        </div>
        <button class="btn primary" :disabled="analyzing" @click="doAnalyze">
          {{ analyzing ? '分析中…' : '统计分析' }}
        </button>
      </template>
      <p v-else class="empty">当前无可图表数值列（考勤类文本台账如实展示表格即可）</p>

      <div v-if="stats" class="stat-grid">
        <div class="stat">
          <div class="stat-value">{{ stats.count }}</div>
          <div class="stat-label">样本数</div>
        </div>
        <div class="stat">
          <div class="stat-value">{{ stats.sum }}</div>
          <div class="stat-label">合计</div>
        </div>
        <div class="stat">
          <div class="stat-value">{{ stats.avg }}</div>
          <div class="stat-label">均值</div>
        </div>
        <div class="stat">
          <div class="stat-value">{{ stats.trend?.direction ?? '—' }}</div>
          <div class="stat-label">趋势</div>
        </div>
      </div>
      <p v-if="stats?.brief" class="muted">{{ stats.brief }}</p>

      <div v-if="rows.length" class="block-gap">
        <button class="btn" :disabled="exporting" @click="doExport">
          {{ exporting ? '导出中…' : '预览 markdown 导出' }}
        </button>
        <button v-if="exportText" class="btn" @click="download">下载 .md</button>
        <pre v-if="exportText" class="export-pre">{{ exportText }}</pre>
      </div>

      <div v-if="pageError" class="result fail">{{ pageError }}</div>
    </section>
  </div>
</template>

<style scoped>
.bars {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 12px 0;
}
.bar-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.bar-cat {
  width: 72px;
  flex-shrink: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.bar-track {
  flex: 1;
  height: 14px;
  background: var(--line);
  border-radius: 7px;
  overflow: hidden;
}
.bar-fill {
  height: 100%;
  background: var(--brand);
  border-radius: 7px;
}
.bar-val {
  width: 72px;
  text-align: right;
  flex-shrink: 0;
}
.export-pre {
  max-height: 240px;
  overflow: auto;
  white-space: pre-wrap;
}
</style>
