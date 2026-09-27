<script setup lang="ts">
// 职责：知识库问答页（PRD §2.6 企业知识库与增强检索）——真实接线：制度答疑/资料检索/跨源联合/图片问答
// 链路：router /knowledge → 本页 → kb.ask / office.kb.search_unified / ocr.image / office.image.ask
// 权限由后端按当前用户角色过滤（visibility public/角色名），前端只展示返回内容
// 对齐：PRD §2.6 + §4.1 权限适配 + AGENTS.md §4 前端红线（禁 mock 兜底，失败置空 + 报错提示）
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { invokeTool } from '../api'

interface KbHit {
  title: string
  snippet: string
  score?: number
  source?: string
  origin?: string
}
interface UnifiedHit extends KbHit {}

const question = ref('')
const qaDialog = ref<{ q: string; answer: KbHit[]; degraded?: boolean; degradedReason?: string }[]>([])
const qaLoading = ref(false)

const searchKw = ref('')
const searchScope = ref<string[]>([])
const searchHits = ref<KbHit[]>([])
const searchLoading = ref(false)

const crossQuery = ref('')
const crossSources = ref<string[]>(['drive', 'im', 'oa', 'pm'])
const mergedHits = ref<UnifiedHit[]>([])
const perSource = ref<Record<string, { count: number; results: UnifiedHit[]; note?: string }>>({})
const crossLoading = ref(false)

const ocrFile = ref('')
const ocrText = ref('')
const ocrMeta = ref<{ format?: string; width?: number; height?: number } | null>(null)
const ocrLoading = ref(false)

const imgQa = reactive({ file_path: '', query: '', language: '' })
const imgFragments = ref<{ snippet: string; score?: number }[]>([])
const imgLoading = ref(false)

const kbTopicTags = ['考勤', '报销', '人事', '行政', '合规']

const _expectOk = (res: unknown) => {
  if (!res || typeof res !== 'object' || (res as { status?: string }).status !== 'ok') {
    throw new Error(res && typeof res === 'object' ? String((res as { result?: unknown }).result ?? '') : '未返回结果')
  }
  return (res as { result?: unknown }).result as Record<string, unknown>
}
const _hitsOf = (payload: Record<string, unknown>): KbHit[] =>
  (payload.results as KbHit[]) ?? (payload.merged as KbHit[]) ?? (payload.answer_fragments as KbHit[]) ?? []

const ask = async (preset?: string) => {
  const q = (preset ?? question.value).trim()
  if (!q) {
    ElMessage.warning('请输入问题')
    return
  }
  question.value = q
  qaLoading.value = true
  try {
    const payload = await _expectOk(await invokeTool('kb.ask', { query: q, top_k: 3 }))
    const hits = _hitsOf(payload)
    qaDialog.value.push({
      q,
      answer: hits.slice(0, 5),
      degraded: Boolean(payload.degraded),
      degradedReason: String(payload.degraded_reason ?? ''),
    })
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    qaLoading.value = false
  }
}

const doSearch = async () => {
  if (!searchKw.value.trim()) {
    ElMessage.warning('请输入关键词')
    return
  }
  searchLoading.value = true
  try {
    const payload = await _expectOk(
      await invokeTool('office.kb.search_unified', {
        query: searchKw.value.trim(),
        top_k: 3,
        sources: searchScope.value.length ? searchScope.value : undefined,
      }),
    )
    searchHits.value = (payload.merged as UnifiedHit[]) ?? []
    if (payload.per_source) {
      perSource.value = payload.per_source as typeof perSource.value
    }
    if (!searchHits.value.length) {
      ElMessage.info(String(payload.degraded_reason ?? '无命中'))
    }
  } catch (e) {
    searchHits.value = []
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    searchLoading.value = false
  }
}

const crossSearch = async () => {
  if (!crossQuery.value.trim()) {
    ElMessage.warning('请输入要查的问题')
    return
  }
  crossLoading.value = true
  try {
    const payload = await _expectOk(
      await invokeTool('office.kb.search_unified', {
        query: crossQuery.value.trim(),
        top_k: 3,
        sources: crossSources.value,
      }),
    )
    mergedHits.value = (payload.merged as UnifiedHit[]) ?? []
    perSource.value = payload.per_source as typeof perSource.value
    if (!mergedHits.value.length) {
      ElMessage.info(String(payload.degraded_reason ?? '各源均无命中'))
    }
  } catch (e) {
    mergedHits.value = []
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    crossLoading.value = false
  }
}

const doOcr = async () => {
  if (!ocrFile.value.trim()) {
    ElMessage.warning('请输入 DOCS_DIR 内的图片文件名（png/jpg/jpeg/bmp/gif/webp）')
    return
  }
  ocrLoading.value = true
  try {
    const payload = await _expectOk(await invokeTool('ocr.image', { file_path: ocrFile.value.trim() }))
    ocrMeta.value = (payload.image as typeof ocrMeta.value) ?? null
    const text = String(payload.text ?? '')
    ocrText.value = text
    if (payload.degraded) {
      ElMessage.warning(String(payload.degraded_reason ?? '识别降级'))
    }
  } catch (e) {
    ocrText.value = ''
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    ocrLoading.value = false
  }
}

const doImgQa = async () => {
  if (!imgQa.file_path.trim() || !imgQa.query.trim()) {
    ElMessage.warning('请填写图片文件名与问题')
    return
  }
  imgLoading.value = true
  try {
    const payload = await _expectOk(
      await invokeTool('office.image.ask', {
        file_path: imgQa.file_path.trim(),
        query: imgQa.query.trim(),
        language: imgQa.language.trim() || undefined,
        top_k: 3,
      }),
    )
    imgFragments.value = ((payload.answer_fragments as { snippet: string; score?: number }[]) ?? []).slice(0, 5)
    if (payload.degraded) {
      ElMessage.warning(String(payload.degraded_reason ?? '图片问答降级'))
    }
  } catch (e) {
    imgFragments.value = []
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    imgLoading.value = false
  }
}

onMounted(() => {
  // 页面载入不做自动查询，避免空跑占用
})
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="page-head">
        <span class="card-title">知识库问答</span>
        <span class="head-hint">制度答疑、资料检索与跨源联合检索，结果严格匹配当前用户权限（PRD §2.6）</span>
      </div>
    </template>
    <el-tabs>
      <el-tab-pane label="制度答疑">
        <div class="ask-bar">
          <el-input v-model="question" placeholder="如：年假怎么请？报销额度多少？" @keyup.enter="ask()">
            <template #append>
              <el-button :loading="qaLoading" @click="ask()">提问</el-button>
            </template>
          </el-input>
        </div>
        <div class="toolbar">
          <el-tag v-for="t in kbTopicTags" :key="t" type="info" class="cursor" @click="ask(t)">{{ t }}</el-tag>
        </div>
        <div v-for="(item, i) in qaDialog" :key="i" class="qa-item">
          <div class="qa-q">问：{{ item.q }}</div>
          <div class="qa-a">
            <template v-if="item.answer.length">
              <div v-for="(h, j) in item.answer" :key="j" class="qa-hit">
                <div class="qa-title">{{ h.title }}</div>
                <div class="qa-snippet">{{ h.snippet }}</div>
              </div>
            </template>
            <el-empty v-else :description="item.degradedReason || '无命中'" :image-size="40" />
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane label="资料检索">
        <el-form inline>
          <el-form-item label="关键词">
            <el-input v-model="searchKw" placeholder="项目资料 / SOP / 历史文档 / 常见问题" style="width: 280px" @keyup.enter="doSearch" />
          </el-form-item>
          <el-form-item label="范围">
            <el-select v-model="searchScope" placeholder="全部来源" multiple clearable style="width: 240px">
              <el-option label="知识库" value="knowledge" />
              <el-option label="网盘文档" value="drive" />
              <el-option label="待办日程" value="affairs" />
              <el-option label="审批单据" value="approval" />
              <el-option label="项目台账" value="project" />
            </el-select>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="searchLoading" @click="doSearch">检索</el-button>
          </el-form-item>
        </el-form>
        <template v-if="searchHits.length">
          <el-table :data="searchHits">
            <el-table-column prop="title" label="标题" min-width="160" />
            <el-table-column prop="snippet" label="命中片段" min-width="320" />
            <el-table-column prop="score" label="分数" width="90" />
            <el-table-column prop="origin" label="来源" width="110" />
          </el-table>
        </template>
      </el-tab-pane>

      <el-tab-pane label="跨源联合检索">
        <div class="toolbar">
          <el-input v-model="crossQuery" placeholder="如：报销审批要找谁" style="width: 320px" @keyup.enter="crossSearch" />
          <el-checkbox-group v-model="crossSources">
            <el-checkbox value="drive">网盘文档</el-checkbox>
            <el-checkbox value="affairs">待办日程</el-checkbox>
            <el-checkbox value="approval">审批单据</el-checkbox>
            <el-checkbox value="project">项目台账</el-checkbox>
          </el-checkbox-group>
          <el-button type="primary" :loading="crossLoading" @click="crossSearch">联合检索</el-button>
        </div>
        <template v-if="mergedHits.length || Object.keys(perSource).length">
          <el-divider content-position="left">合并结果</el-divider>
          <el-table :data="mergedHits">
            <el-table-column prop="title" label="标题" min-width="160" />
            <el-table-column prop="snippet" label="命中片段" min-width="320" />
            <el-table-column prop="score" label="分数" width="90" />
            <el-table-column prop="origin" label="来源" width="110" />
          </el-table>
          <el-divider content-position="left">分源明细</el-divider>
          <div v-for="(val, key) in perSource" :key="key" class="src-block">
            <div class="qa-title">{{ key }}（{{ val.count }}）
              <el-tag v-if="val.note" type="info" size="small">{{ val.note }}</el-tag>
            </div>
            <div v-for="(h, j) in val.results" :key="j" class="qa-snippet">{{ h.title }}：{{ h.snippet }}</div>
          </div>
        </template>
      </el-tab-pane>

      <el-tab-pane label="图片 OCR 问答">
        <div class="toolbar">
          <el-input v-model="ocrFile" placeholder="图片文件名（DOCS_DIR 内，png/jpg 等）" style="width: 300px" @keyup.enter="doOcr" />
          <el-button type="primary" :loading="ocrLoading" @click="doOcr">OCR 识别</el-button>
        </div>
        <template v-if="ocrMeta">
          <el-descriptions :column="4" border size="small" class="block-gap">
            <el-descriptions-item label="格式">{{ ocrMeta.format }}</el-descriptions-item>
            <el-descriptions-item label="尺寸">{{ ocrMeta.width }}×{{ ocrMeta.height }}</el-descriptions-item>
          </el-descriptions>
        </template>
        <pre v-if="ocrText" class="result-block">{{ ocrText }}</pre>
        <el-divider content-position="left">图片内容问答</el-divider>
        <div class="toolbar">
          <el-input v-model="imgQa.file_path" placeholder="图片文件名" style="width: 220px" />
          <el-input v-model="imgQa.query" placeholder="对图片提问，如：报错码是什么" style="width: 320px" @keyup.enter="doImgQa" />
          <el-button type="primary" :loading="imgLoading" @click="doImgQa">问答</el-button>
        </div>
        <div v-for="(f, i) in imgFragments" :key="i" class="qa-hit">
          <div class="qa-snippet">{{ f.snippet }}</div>
        </div>
      </el-tab-pane>
    </el-tabs>
  </el-card>
</template>

<style scoped>
.ask-bar {
  margin-bottom: 12px;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.cursor {
  cursor: pointer;
}
.qa-item {
  margin-bottom: 16px;
}
.qa-q {
  font-weight: 600;
  margin-bottom: 6px;
}
.qa-a {
  padding-left: 12px;
  border-left: 2px solid var(--el-border-color);
}
.qa-hit {
  margin-bottom: 10px;
}
.qa-title {
  font-weight: 600;
  font-size: 13px;
}
.qa-snippet {
  font-size: 13px;
  color: var(--el-text-color-regular);
  line-height: 1.7;
}
.result-block {
  margin: 0;
  padding: 12px 16px;
  background: var(--el-fill-color-light);
  border-radius: 6px;
  white-space: pre-wrap;
  line-height: 1.8;
  font-family: var(--el-font-family);
}
.src-block {
  margin-bottom: 12px;
}
</style>