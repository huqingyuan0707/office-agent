<script setup lang="ts">
// 职责：知识库后台页（PRD §2.13 系统管理与运营后台·知识库后台）——资料上传（PDF/Word/Excel/
//       TXT/Markdown 多选 → 逐个真上传 → 入盘即解析切块向量化，逐文件回执）/ 知识库维护
//       （来源清单 × 入库统计 × 按源文件删除，含搜索与状态如实标注）/ 回答规则（待接口）
// 链路：router /kb-admin → api.listKbFiles/kbStats/uploadKbFile/deleteKbFile（全部仅 admin，
//       非 admin 403 → 壳红条如实提示，不降级给假数据）
// 对齐：AGENTS.md §4 前端红线（`<script setup lang="ts">` + 箭头函数 + 真实接口零 mock +
//       var(--*) token + scoped）；PRD §2.13
import { computed, inject, onMounted, ref } from 'vue'
import type { UploadFile } from 'element-plus'
import { Delete, Refresh, UploadFilled } from '@element-plus/icons-vue'
import { deleteKbFile, kbStats, listKbFiles, uploadKbFile } from '../api'
import type { KbFileList, KbSourceItem, KbStats, KbStoreStatus } from '../api'

const shell = inject('shellError') as {
  showError: (msg: string) => unknown
  clearError: () => unknown
}

//: 与后端 KB_SUFFIXES / MAX_UPLOAD_BYTES 同口径（前端只做前置提示，最终判定在后端）
const ACCEPT = '.pdf,.docx,.xlsx,.csv,.txt,.md'
const MAX_UPLOAD_BYTES = 200_000

//: 向量化口径中文化（后端如实标注的 mode → 人读文案；未知值原样展示不丢信息）
const MODE_LABELS: Record<string, string> = {
  milvus: '已落库向量',
  on_demand_embedding: '检索时按需向量化',
  unconfigured: '未配向量检索（字符通道）',
  unindexed: '未解析',
}
//: 可见范围常用值（allow-create 可自填角色名，最终校验在后端）
const VISIBILITY_OPTIONS = ['public', 'hr', 'admin', 'finance']

interface QueueItem {
  key: string
  file: File
  name: string
  size: number
  status: 'waiting' | 'uploading' | 'done' | 'failed'
  message: string
  chunks: number
}

const activeTab = ref('upload')
const loading = ref(true)
const pageError = ref('')
const listing = ref<KbFileList | null>(null)
const stats = ref<KbStats | null>(null)
const keyword = ref('')

const queue = ref<QueueItem[]>([])
const visibility = ref('public')
const uploading = ref(false)
const busyName = ref('')

const modeLabel = (mode: string) => MODE_LABELS[mode] ?? mode

const formatBytes = (bytes: number) => {
  if (!bytes) return '0 B'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}

// 搜索为本地过滤（清单已一次取全：来源数级量级不需要后端分页口）
const filtered = computed(() => {
  const items = listing.value?.items ?? []
  const key = keyword.value.trim().toLowerCase()
  if (!key) return items
  return items.filter(
    (item) =>
      item.filename.toLowerCase().includes(key) || item.title.toLowerCase().includes(key),
  )
})

const stateTone = (state: KbSourceItem['state']) => {
  if (state === 'indexed') return 'success'
  if (state === 'unindexed') return 'warning'
  return 'danger'
}

const storeText = (store: KbStoreStatus | undefined) =>
  store ? `${modeLabel(store.mode)}：${store.reason}` : ''

const loadAll = async () => {
  loading.value = true
  pageError.value = ''
  shell.clearError()
  try {
    const [files, stat] = await Promise.all([listKbFiles(), kbStats()])
    listing.value = files
    stats.value = stat
  } catch (e) {
    listing.value = null
    stats.value = null
    shell.showError((e as Error).message || '知识库数据加载失败（仅管理员可访问）')
  } finally {
    loading.value = false
  }
}

// 选中文件进待上传队列：格式/体积前置校验（不通过即当场提示，不发无效请求）
const onPick = (uploadFile: UploadFile) => {
  pageError.value = ''
  const raw = uploadFile.raw
  if (!(raw instanceof File)) return
  const suffix = `.${raw.name.split('.').pop()?.toLowerCase() ?? ''}`
  if (!ACCEPT.split(',').includes(suffix)) {
    pageError.value = `「${raw.name}」格式不支持：仅支持 ${ACCEPT}`
    return
  }
  if (raw.size > MAX_UPLOAD_BYTES) {
    pageError.value = `「${raw.name}」超过 ${formatBytes(MAX_UPLOAD_BYTES)} 上限：请拆分后再上传`
    return
  }
  const key = `${raw.name}:${raw.size}:${raw.lastModified}`
  if (queue.value.some((item) => item.key === key)) return
  queue.value.push({
    key,
    file: raw,
    name: raw.name,
    size: raw.size,
    status: 'waiting',
    message: '',
    chunks: 0,
  })
}

const removeFromQueue = (key: string) => {
  queue.value = queue.value.filter((item) => item.key !== key)
}

const clearQueue = () => {
  queue.value = queue.value.filter((item) => item.status === 'uploading')
}

// 逐个上传（串行：单个文件即触发解析 + 向量化，并发会互相抢本地模型与向量库）
const startUpload = async () => {
  const pending = queue.value.filter((item) => item.status !== 'done')
  if (!pending.length) {
    pageError.value = '请先选择要上传的文件'
    return
  }
  uploading.value = true
  pageError.value = ''
  for (const item of pending) {
    item.status = 'uploading'
    item.message = '上传中…'
    try {
      const result = await uploadKbFile(item.file, visibility.value)
      item.status = 'done'
      item.chunks = result.chunks
      item.message = result.degraded
        ? `已入盘未解析：${result.degraded_reason}`
        : `已切 ${result.chunks} 块 · ${modeLabel(result.vector_mode)}` +
          (result.reuploaded ? ' · 覆盖同名来源' : '')
    } catch (e) {
      item.status = 'failed'
      item.message = (e as Error).message || '上传失败'
    }
  }
  uploading.value = false
  await loadAll()
}

// 按源删除：文件 + 该来源全部向量块 + 清单条目一并清理（前端不做二次推断，回执原样展示）
const removeSource = async (raw: unknown) => {
  const row = raw as KbSourceItem
  busyName.value = row.filename
  pageError.value = ''
  try {
    const result = await deleteKbFile(row.filename)
    pageError.value = `已删除「${row.filename}」——${result.vector_reason}`
    await loadAll()
  } catch (e) {
    pageError.value = (e as Error).message || '删除失败'
  } finally {
    busyName.value = ''
  }
}

onMounted(loadAll)
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="page-head">
        <span class="card-title">知识库后台</span>
        <span class="head-hint"
          >上传即解析向量化，删除按源生效（PRD §2.13）；仅管理员可访问</span
        >
        <el-tooltip v-if="listing?.kb_dir" :content="`知识库目录：${listing.kb_dir}`" placement="top">
          <el-tag size="small" type="info" round>{{ listing.kb_dir }}</el-tag>
        </el-tooltip>
      </div>
    </template>

    <el-alert v-if="pageError" :title="pageError" type="info" show-icon closable class="block-gap" />

    <el-tabs v-model="activeTab">
      <el-tab-pane label="资料上传" name="upload">
        <el-upload
          drag
          multiple
          :auto-upload="false"
          :show-file-list="false"
          :accept="ACCEPT"
          :on-change="onPick"
        >
          <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
          <div class="el-upload__text">拖拽文件到此处，或<em>点击选择</em></div>
          <template #tip>
            <div class="upload-hint">
              支持 {{ ACCEPT }}；单文件上限 {{ formatBytes(MAX_UPLOAD_BYTES) }}
              （与解析上限一致，超出请拆分）；可多选后一次提交
            </div>
          </template>
        </el-upload>

        <div class="toolbar">
          <span class="toolbar-label">可见范围</span>
          <el-select
            v-model="visibility"
            filterable
            allow-create
            default-first-option
            style="width: 180px"
          >
            <el-option v-for="v in VISIBILITY_OPTIONS" :key="v" :label="v" :value="v" />
          </el-select>
          <el-button
            type="primary"
            :icon="UploadFilled"
            :loading="uploading"
            @click="startUpload"
            >上传并入库</el-button
          >
          <el-button :disabled="uploading" @click="clearQueue">清空列表</el-button>
        </div>

        <el-table :data="queue" size="small" empty-text="尚未选择文件">
          <el-table-column prop="name" label="文件名" min-width="240" show-overflow-tooltip />
          <el-table-column label="大小" width="110" align="right">
            <template #default="{ row }">{{ formatBytes(row.size) }}</template>
          </el-table-column>
          <el-table-column label="状态" width="110">
            <template #default="{ row }">
              <el-tag
                size="small"
                :type="
                  row.status === 'done'
                    ? 'success'
                    : row.status === 'failed'
                      ? 'danger'
                      : row.status === 'uploading'
                        ? 'primary'
                        : 'info'
                "
              >
                {{
                  { waiting: '待上传', uploading: '上传中', done: '已入库', failed: '失败' }[
                    row.status as string
                  ]
                }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="message" label="入库结果" min-width="260" show-overflow-tooltip />
          <el-table-column label="操作" width="90" fixed="right">
            <template #default="{ row }">
              <el-button
                size="small"
                text
                :disabled="row.status === 'uploading'"
                @click="removeFromQueue(row.key)"
                >移除</el-button
              >
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="知识库维护" name="maintain">
        <div class="kpi-row block-gap">
          <el-statistic title="来源文件数" :value="stats?.summary.files ?? 0" />
          <el-statistic title="已入库" :value="stats?.summary.indexed ?? 0" />
          <el-statistic title="已入盘未解析" :value="stats?.summary.unindexed ?? 0" />
          <el-statistic title="总切片数" :value="stats?.summary.chunks ?? 0" />
          <el-statistic title="已落库向量块" :value="stats?.summary.vectorized_chunks ?? 0" />
          <div class="kpi-text">
            <div class="kpi-text-label">占用空间</div>
            <div class="kpi-text-value">{{ formatBytes(stats?.summary.total_bytes ?? 0) }}</div>
          </div>
        </div>

        <el-alert
          v-if="stats"
          :title="storeText(stats.store)"
          :description="stats.embedding_model ? `向量模型：${stats.embedding_model}` : ''"
          type="warning"
          show-icon
          :closable="false"
          class="block-gap"
        />

        <div class="toolbar">
          <el-input
            v-model="keyword"
            placeholder="按文件名 / 标题搜索"
            clearable
            style="width: 240px"
          />
          <el-button :icon="Refresh" :loading="loading" @click="loadAll">刷新</el-button>
          <span class="head-hint"
            >支持格式：{{ (listing?.supported_formats ?? []).join(' / ') || '—' }}</span
          >
        </div>

        <el-table v-loading="loading" :data="filtered" size="small">
          <el-table-column prop="title" label="标题" min-width="200" show-overflow-tooltip />
          <el-table-column prop="filename" label="源文件" min-width="180" show-overflow-tooltip />
          <el-table-column prop="format" label="格式" width="80" />
          <el-table-column label="大小" width="100" align="right">
            <template #default="{ row }">{{ formatBytes(row.size_bytes) }}</template>
          </el-table-column>
          <el-table-column prop="chunks" label="切片" width="80" align="right" />
          <el-table-column label="向量化" width="180" show-overflow-tooltip>
            <template #default="{ row }">
              <el-tag size="small" :type="row.vectorized ? 'success' : 'info'" effect="light">
                {{ modeLabel(row.vector_mode) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="visibility" label="可见范围" width="100" />
          <el-table-column label="状态" width="120">
            <template #default="{ row }">
              <el-tag size="small" :type="stateTone(row.state)" effect="light" round>
                {{ row.state_label }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="上传" min-width="170" show-overflow-tooltip>
            <template #default="{ row }">
              <span class="mono">{{ row.uploaded_at || '—' }}</span>
              <span v-if="row.uploaded_by"> · {{ row.uploaded_by }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="100" fixed="right">
            <template #default="{ row }">
              <el-popconfirm
                :title="`确认删除「${row.filename}」？文件、来源清单与该源全部向量块将一并清理`"
                confirm-button-text="删除"
                cancel-button-text="取消"
                @confirm="removeSource(row)"
              >
                <template #reference>
                  <el-button
                    size="small"
                    type="danger"
                    text
                    :icon="Delete"
                    :loading="busyName === row.filename"
                    >删除</el-button
                  >
                </template>
              </el-popconfirm>
            </template>
          </el-table-column>
          <template #empty>
            <el-empty
              :image-size="70"
              :description="
                loading ? '加载中…' : keyword ? '没有匹配的来源文件' : '知识库还是空的，先去「资料上传」上传资料'
              "
            />
          </template>
        </el-table>

        <el-table
          v-if="stats?.by_format?.length"
          :data="stats.by_format"
          size="small"
          class="block-gap format-table"
        >
          <el-table-column prop="format" label="格式" width="100" />
          <el-table-column prop="files" label="文件数" width="100" align="right" />
          <el-table-column prop="chunks" label="切片数" width="100" align="right" />
          <el-table-column label="占用空间" width="120" align="right">
            <template #default="{ row }">{{ formatBytes(row.bytes) }}</template>
          </el-table-column>
          <el-table-column label="来源溯源">
            <template #default="{ row }">
              <span class="mono">{{ stats?.source }} · {{ stats?.fetched_at }}</span>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="回答规则" name="rules">
        <el-form label-width="96px" style="max-width: 520px">
          <el-form-item label="相似度阈值">
            <el-slider :model-value="0.35" disabled />
          </el-form-item>
          <el-form-item label="无答案话术">
            <el-input placeholder="检索不到依据时的统一回复" disabled />
          </el-form-item>
          <el-form-item label="引用展示">
            <el-switch disabled />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" disabled>保存规则</el-button>
          </el-form-item>
        </el-form>
        <el-empty description="功能建设中——回答规则读写接口待补（当前阈值由服务端 EMBEDDING_MIN_SCORE 决定）" />
      </el-tab-pane>
    </el-tabs>
  </el-card>
</template>

<style scoped>
.toolbar {
  display: flex;
  gap: 12px;
  margin: 12px 0;
  align-items: center;
  flex-wrap: wrap;
}
.toolbar-label {
  color: var(--sub);
  font-size: 13px;
}
.upload-hint {
  color: var(--muted);
  font-size: 12px;
}
.kpi-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
}
.kpi-text-label {
  margin-bottom: 4px;
  color: var(--sub);
  font-size: 13px;
}
.kpi-text-value {
  font-size: 20px;
  font-weight: 600;
  line-height: 1.4;
}
.format-table {
  margin-top: 16px;
}
</style>