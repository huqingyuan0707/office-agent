<script setup lang="ts">
// 职责：多模态工作台（PRD §2.12 多模态能力）——真实接线：业务截图/报表图解读问答 + 一键 PPT 生成
// 链路：router /media → 本页 → invokeTool office.image.ask / office.pptx.generate
// PPT 为写动作恒送审：idem_key 前端按 web 前缀生成，复核员批准后才写盘，到 DOCS_DIR 下载
// 对齐：PRD §2.12 + AGENTS.md §4 前端红线（禁 mock 兜底，失败置空 + 报错提示）
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { invokeTool } from '../api'

// ---------- 图片解读 ----------
const imgForm = reactive({
  file_name: '',
  query: '',
  language: '',
})
const imgResult = ref<{
  file?: string
  image?: { format?: string; width?: number; height?: number }
  answer_fragments?: { snippet: string; score?: number }[]
  degraded_reason?: string
  retrieval_mode?: string
} | null>(null)
const imgLoading = ref(false)

// ---------- PPT 生成 ----------
const useType = ref<'outline' | 'text'>('outline')
const pptForm = reactive({
  title: '',
  filename: '',
  outline: '',
  source_text: '',
})
const slides = ref<{ title: string; bullets: string[] }[]>([])
const pptHint = ref('')
const pptLoading = ref(false)

const _expectOk = (res: unknown) => {
  if (!res || typeof res !== 'object' || (res as { status?: string }).status !== 'ok') {
    throw new Error(res && typeof res === 'object' ? String((res as { result?: unknown }).result ?? '') : '未返回结果')
  }
  return (res as { result?: unknown }).result as Record<string, unknown>
}

const addSlide = () => slides.value.push({ title: '', bullets: [] })
const addBullet = (i: number) => slides.value[i].bullets.push('')
const removeSlide = (i: number) => slides.value.splice(i, 1)
const removeBullet = (si: number, bi: number) => slides.value[si].bullets.splice(bi, 1)

const pushOutline = () => {
  const lines = pptForm.outline
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
  for (const line of lines) {
    const [title, ...rest] = line.split(/[：:\-]/)
    if (!title) continue
    slides.value.push({ title: title.trim(), bullets: rest.join(':').split(/[;；]/).map((b) => b.trim()).filter(Boolean) })
  }
  pptForm.outline = ''
}

const pptFromText = async () => {
  const text = pptForm.source_text.trim()
  if (!text) {
    ElMessage.warning('请粘贴要转 PPT 的原始文本')
    return
  }
  pptLoading.value = true
  try {
    const payload = await _expectOk(
      await invokeTool('office.text.summarize', { text, max_sentences: 3 }),
    )
    const summary = String(payload.summary ?? payload.joint_summary ?? '')
    if (!summary) {
      ElMessage.warning('摘要为空，无法生成大纲')
      return
    }
    slides.value = summary
      .split('\n')
      .map((l) => l.trim())
      .filter(Boolean)
      .slice(0, 8)
      .map((s) => ({ title: s.slice(0, 18), bullets: [s] }))
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    pptLoading.value = false
  }
}

const generatePpt = async () => {
  const used = slides.value.filter((s) => s.title.trim() || s.bullets.some((b) => b.trim()))
  if (!pptForm.title.trim() || !used.length) {
    ElMessage.warning('请填写演示文稿标题并至少添加一页大纲')
    return
  }
  pptLoading.value = true
  pptHint.value = ''
  try {
    const res = await invokeTool('office.pptx.generate', {
      title: pptForm.title.trim(),
      filename: pptForm.filename.trim() || `${pptForm.title.trim()}.pptx`,
      slides: used.map((s) => ({ title: s.title.trim(), bullets: s.bullets.map((b) => b.trim()).filter(Boolean) })),
      idem_key: `web-${Date.now()}`,
    })
    if (res.requires_approval) {
      pptHint.value = `已提交审批（单号 ${res.approval_id || '-'}），复核员批准后生成 .pptx 到文档工作目录`
    } else {
      pptHint.value = 'PPT 已生成'
    }
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    pptLoading.value = false
  }
}

const askImage = async () => {
  if (!imgForm.file_name.trim() || !imgForm.query.trim()) {
    ElMessage.warning('请填写图片文件名与提问内容')
    return
  }
  imgLoading.value = true
  try {
    const payload = await _expectOk(
      await invokeTool('office.image.ask', {
        file_path: imgForm.file_name.trim(),
        query: imgForm.query.trim(),
        language: imgForm.language.trim() || undefined,
        top_k: 3,
      }),
    )
    imgResult.value = payload as typeof imgResult.value
    if (payload.degraded) {
      ElMessage.warning(String(payload.degraded_reason ?? '图片问答降级'))
    }
  } catch (e) {
    imgResult.value = null
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    imgLoading.value = false
  }
}
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="page-head">
        <span class="card-title">多模态工作台</span>
        <span class="head-hint">业务截图解读与一键 PPT 生成（PRD §2.12）</span>
      </div>
    </template>
    <el-tabs>
      <el-tab-pane label="图片解读">
        <div class="toolbar">
          <el-input v-model="imgForm.file_name" placeholder="图片文件名（内含文字/报表）" style="width: 360px" />
          <el-input v-model="imgForm.query" placeholder="提问：这张报表说明了什么？" style="flex: 1" @keyup.enter="askImage" />
          <el-button type="primary" :loading="imgLoading" @click="askImage">解读</el-button>
        </div>
        <el-alert
          type="info"
          :closable="false"
          title="说明：文本型图片走 OCR + 三级降级检索匹配片段；可视图表解读依赖视觉大模型，缺失时如实降级"
          class="ops-gap"
        />
        <template v-if="imgResult">
          <el-descriptions v-if="imgResult.image" :column="4" border size="small" class="ops-gap">
            <el-descriptions-item label="文件">{{ imgResult.file }}</el-descriptions-item>
            <el-descriptions-item label="格式">{{ imgResult.image.format }}</el-descriptions-item>
            <el-descriptions-item label="尺寸">{{ imgResult.image.width }}×{{ imgResult.image.height }}</el-descriptions-item>
            <el-descriptions-item label="检索">{{ imgResult.retrieval_mode }}</el-descriptions-item>
          </el-descriptions>
          <div v-for="(f, i) in imgResult.answer_fragments" :key="i" class="img-frag">
            {{ f.snippet }}
            <el-tag v-if="f.score !== undefined" size="small" style="margin-left: 8px">{{ f.score }}</el-tag>
          </div>
          <el-empty v-if="!imgResult.answer_fragments?.length" :description="imgResult.degraded_reason || '无命中'" :image-size="50" />
        </template>
      </el-tab-pane>

      <el-tab-pane label="PPT 生成">
        <el-form label-width="88px" style="max-width: 640px">
          <el-form-item label="内容来源">
            <el-radio-group v-model="useType">
              <el-radio value="outline">自行编排大纲</el-radio>
              <el-radio value="text">文本自动提炼</el-radio>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="标题">
            <el-input v-model="pptForm.title" placeholder="演示文稿标题（同时作封面）" />
          </el-form-item>
          <el-form-item label="文件名">
            <el-input v-model="pptForm.filename" placeholder="缺省为标题.pptx，写动作批准后落 DOCS_DIR" />
          </el-form-item>

          <el-form-item v-if="useType === 'text'" label="原始文本">
            <el-input v-model="pptForm.source_text" type="textarea" :rows="6" placeholder="粘贴要转 PPT 的文本，自动摘要后生成大纲" />
          </el-form-item>
          <el-form-item v-if="useType === 'text'">
            <el-button :loading="pptLoading" @click="pptFromText">提炼大纲</el-button>
          </el-form-item>

          <el-form-item v-if="useType === 'outline'" label="快捷生成">
            <el-input v-model="pptForm.outline" type="textarea" :rows="3" placeholder="每行一页：标题：要点1;要点2;…" style="margin-bottom: 4px" />
            <div style="width: 100%">
              <el-button link type="primary" @click="pushOutline">解析为大纲</el-button>
            </div>
          </el-form-item>

          <el-divider />
          <el-form-item label="大纲页">
            <div style="width: 100%">
              <div v-for="(s, si) in slides" :key="si" class="slide-card">
                <div class="slide-head">
                  <el-input v-model="s.title" placeholder="第 {{ si + 1 }} 页标题" />
                  <el-button link type="danger" @click="removeSlide(si)">删页</el-button>
                </div>
                <div v-for="(b, bi) in s.bullets" :key="bi" class="bullet-row">
                  <el-input v-model="s.bullets[bi]" :placeholder="'要点 ' + (bi + 1)" />
                  <el-button link type="danger" @click="removeBullet(si, bi)">删</el-button>
                </div>
                <el-button link type="primary" @click="addBullet(si)">+ 要点</el-button>
              </div>
              <el-button link type="primary" @click="addSlide">+ 添加页</el-button>
            </div>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="pptLoading" @click="generatePpt">生成 PPT（写动作·需审批）</el-button>
          </el-form-item>
        </el-form>
        <el-alert v-if="pptHint" :title="pptHint" type="success" show-icon :closable="false" class="ops-gap" />
      </el-tab-pane>
    </el-tabs>
  </el-card>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.ops-gap {
  margin: 12px 0;
}
.img-frag {
  padding: 10px 12px;
  border-left: 2px solid var(--el-color-primary);
  background: var(--el-fill-color-light);
  border-radius: 4px;
  margin-bottom: 8px;
  font-size: 13px;
  line-height: 1.7;
}
.slide-card {
  border: 1px solid var(--el-border-color);
  border-radius: 6px;
  padding: 8px;
  margin-bottom: 8px;
}
.slide-head {
  display: flex;
  gap: 8px;
  margin-bottom: 4px;
}
.bullet-row {
  display: flex;
  gap: 8px;
  margin-bottom: 4px;
}
</style>