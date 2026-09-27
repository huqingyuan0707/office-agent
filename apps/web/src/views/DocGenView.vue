<script setup lang="ts">
// 职责：文档中心页（PRD §2.1 智能内容生成与文档处理）——真实接线：自动生成/处理/解析/对比/批量/模板/术语
// 链路：router /docs → 本页 → invokeTool office.memo.compose / text.* / file.read / doc.compare /
//       docs.rename / template.* / terms.translate；docx 下载走 office.docx.render base64 → Blob
// 写动作恒送审：docs.rename / template.save 由后端审批闸门把关，idem_key 前端按 web 前缀生成
// 对齐：PRD §2.1 + AGENTS.md §4 前端红线（禁 mock 兜底，失败置空 + 报错提示）
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { invokeTool } from '../api'

// ---------- 自动生成 ----------
const composeKindMap: Record<string, string> = {
  notice: '通知公告',
  email: '邮件',
  proposal: '方案草稿',
  summary: '工作总结',
  briefing: '工作汇报',
}
const composeTips: Record<string, string> = {
  notice: '通知对象、事项、时间地点、要求',
  email: '收件人、事由、正文要点、落款',
  proposal: '背景、目标、实施步骤、资源需求',
  summary: '本期成果、数据支撑、问题、下期计划',
  briefing: '进展、关键数据、风险、请示事项',
}
const composeForm = reactive({
  kind: 'summary',
  title: '',
  points: [] as string[],
})
const composeText = ref('')
const composeLoading = ref(false)

// ---------- 文档处理 ----------
const opMode = ref<'digest' | 'format'>('digest')
const textIn = ref('')
const textOut = ref('')
const textLoading = ref(false)

// ---------- 文件解析 ----------
const readFile = ref('')
const readText = ref('')
const readLoading = ref(false)

// ---------- 文档对比 ----------
const compareForm = reactive({ file_a: '', file_b: '' })
const compareResult = ref<{
  change_summary?: string
  counts?: Record<string, number>
  details?: { added?: { text: string }[]; removed?: { text: string }[]; changed?: { from?: string; to?: string }[] }
} | null>(null)
const compareLoading = ref(false)

// ---------- 批量处理 ----------
const renamePairs = ref<{ src: string; dst: string }[]>([])
const renameHint = ref('')

// ---------- 自定义模板 ----------
const tplForm = reactive({ name: '', title: '', sections: '' })
const tplApplyName = ref('')
const tplApplyValues = ref('')
const tplResult = ref('')
const tplSaving = ref(false)
const tplApplying = ref(false)

// ---------- 术语翻译 ----------
const termsIn = ref('')
const termsOut = ref('')
const termsLoading = ref(false)

const _expectOk = (res: unknown) => {
  if (!res || typeof res !== 'object' || (res as { status?: string }).status !== 'ok') {
    throw new Error(res && typeof res === 'object' ? String((res as { result?: unknown }).result ?? '') : '未返回结果')
  }
  return (res as { result?: unknown }).result as Record<string, unknown>
}

// ---------- 自动生成 ----------
const runCompose = async () => {
  if (!composeForm.title.trim() || !composeForm.points.length) {
    ElMessage.warning('请填写文案标题并至少添加一个要点')
    return
  }
  composeLoading.value = true
  try {
    const payload = await _expectOk(
      await invokeTool('office.memo.compose', {
        kind: composeForm.kind,
        title: composeForm.title.trim(),
        points: composeForm.points,
      }),
    )
    composeText.value = String(payload.content ?? payload.document ?? payload.text ?? '')
    if (!composeText.value) ElMessage.error('文案生成未返回内容')
  } catch (e) {
    composeText.value = ''
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    composeLoading.value = false
  }
}

const downloadDocx = async () => {
  if (!composeText.value) return
  try {
    const payload = await _expectOk(
      await invokeTool('office.docx.render', {
        title: composeForm.title.trim() || '未命名文档',
        markdown: composeText.value,
      }),
    )
    const b64 = String(payload.content ?? '')
    if (!b64) {
      ElMessage.error('Word 渲染未返回内容')
      return
    }
    const blob = new Blob(
      [Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))],
      { type: String(payload.mime ?? 'application/vnd.openxmlformats-officedocument.wordprocessingml.document') },
    )
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${composeForm.title.trim() || '文档'}.docx`
    a.click()
    URL.revokeObjectURL(url)
    ElMessage.success('Word 文档已生成并下载')
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  }
}

// ---------- 文档处理 ----------
const runTextOp = async () => {
  if (!textIn.value.trim()) {
    ElMessage.warning('请粘贴待处理文本')
    return
  }
  textLoading.value = true
  try {
    if (opMode.value === 'digest') {
      const payload = await _expectOk(
        await invokeTool('office.text.summarize', {
          text: textIn.value,
          max_sentences: 3,
        }),
      )
      textOut.value = String(payload.summary ?? payload.joint_summary ?? '')
    } else {
      const payload = await _expectOk(
        await invokeTool('office.text.normalize', { text: textIn.value }),
      )
      textOut.value = String(payload.normalized ?? '')
    }
    if (!textOut.value) ElMessage.error('处理未返回内容')
  } catch (e) {
    textOut.value = ''
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    textLoading.value = false
  }
}

// ---------- 文件解析 ----------
const extractFile = async () => {
  if (!readFile.value.trim()) {
    ElMessage.warning('请输入 DOCS_DIR 内的文件名（docx/xlsx/csv/txt/md/pdf）')
    return
  }
  readLoading.value = true
  try {
    const payload = await _expectOk(
      await invokeTool('office.file.read', { filename: readFile.value.trim() }),
    )
    readText.value = String(payload.text ?? payload.content ?? '')
    if (!readText.value) ElMessage.error('文件解析未返回内容')
  } catch (e) {
    readText.value = ''
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    readLoading.value = false
  }
}

// ---------- 文档对比 ----------
const runCompare = async () => {
  if (!compareForm.file_a.trim() || !compareForm.file_b.trim()) {
    ElMessage.warning('请输入两份 docx 文件名')
    return
  }
  compareLoading.value = true
  try {
    compareResult.value = (await _expectOk(
      await invokeTool('office.doc.compare', {
        file_a: compareForm.file_a.trim(),
        file_b: compareForm.file_b.trim(),
      }),
    )) as typeof compareResult.value
  } catch (e) {
    compareResult.value = null
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    compareLoading.value = false
  }
}

// ---------- 批量处理 ----------
const addRenameRow = () => renamePairs.value.push({ src: '', dst: '' })
const removeRenameRow = (i: number) => renamePairs.value.splice(i, 1)
const runRename = async () => {
  const pairs = renamePairs.value.filter((p) => p.src.trim() && p.dst.trim())
  if (!pairs.length) {
    ElMessage.warning('请至少填写一对 原文件名 → 新文件名')
    return
  }
  try {
    const res = await invokeTool('office.docs.rename', {
      pairs: pairs.map((p) => ({ src: p.src.trim(), dst: p.dst.trim() })),
      idem_key: `web-${Date.now()}`,
    })
    if (res.requires_approval) {
      renameHint.value = `已提交审批（单号 ${res.approval_id || '-'}），复核员批准后执行改名`
    } else {
      renameHint.value = '批量重命名已完成'
    }
  } catch (e) {
    renameHint.value = ''
    ElMessage.error(e instanceof Error ? e.message : String(e))
  }
}

// ---------- 自定义模板 ----------
const saveTemplate = async () => {
  const sections = tplForm.sections
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean)
  if (!tplForm.name.trim() || !sections.length) {
    ElMessage.warning('请填写模板名与至少一段模板内容')
    return
  }
  tplSaving.value = true
  try {
    const res = await invokeTool('office.template.save', {
      name: tplForm.name.trim(),
      title: tplForm.title.trim() || undefined,
      sections,
      idem_key: `web-${Date.now()}`,
    })
    if (res.requires_approval) {
      ElMessage.info(`模板保存已提交审批（单号 ${res.approval_id || '-'}），批准后入库`)
    } else {
      ElMessage.success('模板已保存')
    }
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    tplSaving.value = false
  }
}

const applyTemplate = async () => {
  if (!tplApplyName.value.trim()) {
    ElMessage.warning('请填写要复用的模板名')
    return
  }
  tplApplying.value = true
  try {
    let values: Record<string, string> | undefined
    if (tplApplyValues.value.trim()) {
      try {
        values = JSON.parse(tplApplyValues.value) as Record<string, string>
      } catch {
        ElMessage.warning('占位符取值需为 JSON 对象，如 {"本周工作": "..."}')
        return
      }
    }
    const payload = await _expectOk(
      await invokeTool('office.template.apply', {
        name: tplApplyName.value.trim(),
        values,
      }),
    )
    tplResult.value = String(payload.filled ?? payload.document ?? payload.content ?? '')
    if (payload.unfilled) {
      tplResult.value += `\n\n（未填充占位符：${String(payload.unfilled)}）`
    }
  } catch (e) {
    tplResult.value = ''
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    tplApplying.value = false
  }
}

// ---------- 术语翻译 ----------
const runTranslate = async () => {
  if (!termsIn.value.trim()) {
    ElMessage.warning('请输入待统一术语的文本')
    return
  }
  termsLoading.value = true
  try {
    const payload = await _expectOk(
      await invokeTool('office.terms.translate', { text: termsIn.value }),
    )
    termsOut.value = String(payload.out ?? payload.result ?? payload.text ?? '')
    if (!termsOut.value) ElMessage.error('术语翻译未返回内容')
  } catch (e) {
    termsOut.value = ''
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    termsLoading.value = false
  }
}
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="page-head">
        <span class="card-title">文档中心</span>
        <span class="head-hint">内容生成与文档处理统一入口（PRD §2.1）</span>
      </div>
    </template>
    <el-tabs>
      <el-tab-pane label="自动生成">
        <el-form label-width="72px">
          <el-form-item label="文档类型">
            <el-select v-model="composeForm.kind" style="width: 180px">
              <el-option v-for="(label, val) in composeKindMap" :key="val" :label="label" :value="val" />
            </el-select>
          </el-form-item>
          <el-form-item label="标题">
            <el-input v-model="composeForm.title" placeholder="如：2026 年 9 月工作总结" />
          </el-form-item>
          <el-form-item label="要点">
            <el-select
              v-model="composeForm.points"
              multiple
              filterable
              allow-create
              default-first-option
              placeholder="输入要点后回车，如：完成数据看板上线"
              style="width: 100%"
            />
            <div class="muted" style="width: 100%">{{ composeTips[composeForm.kind] }}</div>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="composeLoading" @click="runCompose">生成草稿</el-button>
            <el-button v-if="composeText" :disabled="composeLoading" @click="downloadDocx">下载 Word</el-button>
          </el-form-item>
        </el-form>
        <pre v-if="composeText" class="result-block">{{ composeText }}</pre>
      </el-tab-pane>

      <el-tab-pane label="文档处理">
        <el-radio-group v-model="opMode">
          <el-radio-button value="digest">长文摘要</el-radio-button>
          <el-radio-button value="format">格式统一</el-radio-button>
        </el-radio-group>
        <el-input v-model="textIn" type="textarea" :rows="5" placeholder="粘贴或上传待处理文本" class="ops-gap" />
        <el-button type="primary" :loading="textLoading" @click="runTextOp">
          {{ opMode === 'digest' ? '生成摘要' : '统一格式' }}
        </el-button>
        <pre v-if="textOut" class="result-block">{{ textOut }}</pre>
        <p class="muted" style="margin-top: 8px">
          摘要为抽取式原文原句拼接（不润色改写）；润色改写需大模型，已如实后置。
        </p>
      </el-tab-pane>

      <el-tab-pane label="文件解析">
        <div class="toolbar">
          <el-input v-model="readFile" placeholder="DOCS_DIR 内文件名（docx/xlsx/csv/txt/md/pdf）" style="width: 320px" @keyup.enter="extractFile" />
          <el-button type="primary" :loading="readLoading" @click="extractFile">提取文档内容</el-button>
        </div>
        <pre v-if="readText" class="result-block">{{ readText }}</pre>
      </el-tab-pane>

      <el-tab-pane label="文档对比">
        <div class="grid2">
          <el-input v-model="compareForm.file_a" placeholder="基准版本文档（.docx 文件名）">
            <template #prepend>版本 A</template>
          </el-input>
          <el-input v-model="compareForm.file_b" placeholder="对比版本文档（.docx 文件名）">
            <template #prepend>版本 B</template>
          </el-input>
        </div>
        <el-button type="primary" :loading="compareLoading" @click="runCompare">对比差异</el-button>
        <template v-if="compareResult">
          <el-alert :title="compareResult.change_summary" type="info" show-icon :closable="false" class="block-gap" />
          <div class="block-gap">
            <el-tag v-for="(v, k) in compareResult.counts" :key="k" style="margin-right: 8px">
              {{ { added: '新增', removed: '删除', changed: '修改', unchanged: '未变' }[k] ?? k }}：{{ v }}
            </el-tag>
          </div>
          <el-descriptions :column="1" border class="block-gap">
            <el-descriptions-item label="新增段落">
              <template v-if="compareResult.details?.added?.length">
                <div v-for="(item, i) in compareResult.details.added" :key="i" class="diff-line add">+ {{ item.text }}</div>
              </template>
              <span v-else class="muted">无</span>
            </el-descriptions-item>
            <el-descriptions-item label="删除段落">
              <template v-if="compareResult.details?.removed?.length">
                <div v-for="(item, i) in compareResult.details.removed" :key="i" class="diff-line del">- {{ item.text }}</div>
              </template>
              <span v-else class="muted">无</span>
            </el-descriptions-item>
            <el-descriptions-item label="修改段落">
              <template v-if="compareResult.details?.changed?.length">
                <div v-for="(item, i) in compareResult.details.changed" :key="i" class="diff-line mod">
                  ~ {{ item.from }} → {{ item.to }}
                </div>
              </template>
              <span v-else class="muted">无</span>
            </el-descriptions-item>
          </el-descriptions>
        </template>
      </el-tab-pane>

      <el-tab-pane label="批量处理">
        <p class="muted">批量重命名（送审）：原文件名 → 新文件名，均不含路径</p>
        <div v-for="(row, i) in renamePairs" :key="i" class="rename-row">
          <el-input v-model="row.src" placeholder="原文件名（如 季度报告.docx）" />
          <el-input v-model="row.dst" placeholder="新文件名（如 Q3季度报告.docx）" />
          <el-button link type="danger" @click="removeRenameRow(i)">删除</el-button>
        </div>
        <div class="toolbar">
          <el-button link type="primary" @click="addRenameRow">+ 添加改名对</el-button>
          <el-button type="primary" @click="runRename">批量重命名</el-button>
        </div>
        <el-alert v-if="renameHint" :title="renameHint" type="success" show-icon :closable="false" />
      </el-tab-pane>

      <el-tab-pane label="自定义模板">
        <el-form label-width="88px" style="max-width: 640px">
          <el-form-item label="模板名">
            <el-input v-model="tplForm.name" placeholder="如：周报模板" />
          </el-form-item>
          <el-form-item label="模板标题">
            <el-input v-model="tplForm.title" placeholder="可含 {占位符}，如 {日期} 周报" />
          </el-form-item>
          <el-form-item label="模板内容">
            <el-input v-model="tplForm.sections" type="textarea" :rows="4" placeholder="每行一段，可含 {占位符}" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="tplSaving" @click="saveTemplate">保存模板</el-button>
          </el-form-item>
        </el-form>
        <el-divider content-position="left">复用模板</el-divider>
        <el-form label-width="88px" style="max-width: 640px">
          <el-form-item label="模板名">
            <el-input v-model="tplApplyName" placeholder="如：周报模板" />
          </el-form-item>
          <el-form-item label="占位取值">
            <el-input v-model="tplApplyValues" placeholder='JSON，如 {"日期": "2026-09-27", "本周工作": "..."}' />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="tplApplying" @click="applyTemplate">填充生成</el-button>
          </el-form-item>
        </el-form>
        <pre v-if="tplResult" class="result-block">{{ tplResult }}</pre>
      </el-tab-pane>

      <el-tab-pane label="术语翻译">
        <el-input v-model="termsIn" type="textarea" :rows="4" placeholder="输入待统一术语的文本，专业名词按内部术语库（内置词表 + DOCS_DIR/terms.csv）最长匹配替换" />
        <el-button type="primary" :loading="termsLoading" class="ops-gap" @click="runTranslate">统一术语</el-button>
        <pre v-if="termsOut" class="result-block">{{ termsOut }}</pre>
      </el-tab-pane>
    </el-tabs>
  </el-card>
</template>

<style scoped>
.ops-gap {
  margin: 12px 0;
}
.grid2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-bottom: 12px;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.result-block {
  margin: 12px 0 0;
  padding: 12px 16px;
  background: var(--el-fill-color-light);
  border-radius: 6px;
  white-space: pre-wrap;
  line-height: 1.8;
  font-family: var(--el-font-family);
}
.rename-row {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}
.diff-line {
  font-size: 13px;
  line-height: 1.7;
  word-break: break-all;
}
.diff-line.add {
  color: var(--el-color-success);
}
.diff-line.del {
  color: var(--el-color-danger);
}
.diff-line.mod {
  color: var(--el-color-warning);
}
</style>