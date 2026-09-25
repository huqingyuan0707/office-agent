<script setup lang="ts">
// 职责：邮件与消息页（PRD §2.7）—— 单封邮件归类 + 行动项提取、对外邮件预审、群消息摘要，
//       全部走后端真实工具（classify/action_items/precheck/digest），入参驱动零 mock
// 链路：router /mail → api.invokeTool；失败 → 本页红条 + 结果置空（各 tab 互不影响）
// 对齐：AGENTS.md §4 前端红线（真实接口零 mock、箭头函数、var(--*) token + scoped）+ PRD §2.7
import { inject, ref } from 'vue'
import { invokeTool } from '../api'

interface ClassifyItem {
  category: string
  category_label: string
  matched_keywords: string[]
}
interface ActionItem {
  action: string
  owner: string | null
  due: string | null
}
interface PrecheckHit {
  category: string
  rule: string
  excerpt: string
  position: number
}
interface DigestTask {
  task: string
  from: string
  due: string | null
}
interface Digest {
  brief?: string
  my_tasks?: DigestTask[]
  mentions?: { sender: string; text: string }[]
  questions?: string[]
  decisions?: string[]
  risks?: string[]
}

const shell = inject('shellError') as {
  showError: (msg: string) => unknown
  clearError: () => unknown
}

// ---------- tab1：单封归类 + 行动项 ----------
const subject = ref('')
const body = ref('')
const classifyResult = ref<ClassifyItem | null>(null)
const actionItems = ref<ActionItem[]>([])
const tab1Busy = ref(false)
const pageError = ref('') // 本页级共享红条（左/右卡错误都进这里，失败绝不拿假数据顶）

const doClassify = async () => {
  if (!subject.value.trim() && !body.value.trim()) return
  tab1Busy.value = true
  pageError.value = ''
  try {
    const cls = await invokeTool('office.mail.classify', {
      emails: [{ subject: subject.value, body: body.value }],
    })
    classifyResult.value = ((cls.result ?? {}) as { results?: ClassifyItem[] }).results?.[0] ?? null
    const acts = await invokeTool('office.mail.action_items', { body: body.value })
    actionItems.value = ((acts.result ?? {}) as { items?: ActionItem[] }).items ?? []
  } catch (e) {
    classifyResult.value = null
    actionItems.value = []
    pageError.value = (e as Error).message || '邮件处理失败'
  } finally {
    tab1Busy.value = false
  }
}

// ---------- tab2：对外预审 ----------
const precheckText = ref('')
const precheckPassed = ref<boolean | null>(null)
const precheckHits = ref<PrecheckHit[]>([])
const precheckTips = ref<string[]>([])
const tab2Busy = ref(false)

const doPrecheck = async () => {
  if (!precheckText.value.trim()) return
  tab2Busy.value = true
  pageError.value = ''
  try {
    const data = await invokeTool('office.mail.precheck', { text: precheckText.value })
    const result = (data.result ?? {}) as {
      passed?: boolean
      hits?: PrecheckHit[]
      tips?: string[]
    }
    precheckPassed.value = result.passed ?? null
    precheckHits.value = result.hits ?? []
    precheckTips.value = result.tips ?? []
  } catch (e) {
    precheckPassed.value = null
    pageError.value = (e as Error).message || '预审失败'
  } finally {
    tab2Busy.value = false
  }
}

// ---------- tab3：群消息摘要 ----------
const me = ref('')
const imSource = ref<string>('feishu')
const messagesText = ref('')
const digest = ref<Digest | null>(null)
const tab3Busy = ref(false)

const doDigest = async () => {
  const messages = messagesText.value
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.includes(':') || line.includes('：'))
    .map((line) => {
      const at = line.search(/[:：]/)
      return { sender: line.slice(0, at).trim(), text: line.slice(at + 1).trim() }
    })
    .filter((m) => m.sender && m.text)
  if (!messages.length || !me.value.trim()) {
    pageError.value = '请填写本人在群里的称呼，并按“发送人: 内容”每行一条粘贴消息'
    return
  }
  tab3Busy.value = true
  pageError.value = ''
  try {
    const data = await invokeTool('office.im.digest', {
      messages,
      me: me.value.trim(),
      source: imSource.value,
    })
    digest.value = (data.result ?? {}) as Digest
  } catch (e) {
    digest.value = null
    pageError.value = (e as Error).message || '摘要失败'
  } finally {
    tab3Busy.value = false
  }
}
</script>

<template>
  <div class="grid">
    <el-card shadow="never">
      <template #header>
        <div class="page-head">
          <span class="card-title">邮件处理</span>
        </div>
      </template>
      <el-input v-model="subject" placeholder="邮件主题" class="block-gap" />
      <el-input v-model="body" type="textarea" :rows="5" placeholder="邮件正文（行动项从正文提取）" class="block-gap" />
      <el-button type="primary" :loading="tab1Busy" @click="doClassify">归类 + 提取行动项</el-button>
      <div v-if="classifyResult" class="block-gap">
        <el-tag effect="light" round>{{ classifyResult.category_label }}</el-tag>
        <span class="muted">命中：{{ classifyResult.matched_keywords.join('、') || '无（一般告知）' }}</span>
      </div>
      <el-table v-if="actionItems.length" :data="actionItems" size="small" class="block-gap">
        <el-table-column prop="action" label="行动项" min-width="200" show-overflow-tooltip />
        <el-table-column prop="owner" label="责任人" width="110">
          <template #default="{ row }">{{ row.owner ?? '未给出' }}</template>
        </el-table-column>
        <el-table-column prop="due" label="截止" width="120">
          <template #default="{ row }">{{ row.due ?? '未给出' }}</template>
        </el-table-column>
      </el-table>
      <p class="muted">转待办请到待办页走 office.todo.create（写动作恒送审，本页只出清单不落库）。</p>
      <el-alert v-if="pageError" class="block-gap" type="error" :title="pageError" show-icon :closable="false" />
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="page-head">
          <span class="card-title">对外预审 / 群摘要</span>
        </div>
      </template>
      <span class="sub-title">对外邮件预审</span>
      <el-input v-model="precheckText" type="textarea" :rows="4" placeholder="粘贴拟外发正文" class="block-gap" />
      <el-button type="primary" :loading="tab2Busy" @click="doPrecheck">预审检查</el-button>
      <div v-if="precheckPassed !== null" class="block-gap">
        <el-tag :type="precheckPassed ? 'success' : 'danger'" effect="light" round>
          {{ precheckPassed ? '通过' : '未通过，需二次确认' }}
        </el-tag>
      </div>
      <el-table v-if="precheckHits.length" :data="precheckHits" size="small" class="block-gap">
        <el-table-column prop="rule" label="规则" width="140" />
        <el-table-column prop="excerpt" label="命中片段" min-width="160" show-overflow-tooltip />
      </el-table>
      <p v-for="tip in precheckTips" :key="tip" class="muted">{{ tip }}</p>

      <el-divider />
      <span class="sub-title">群消息摘要</span>
      <div class="toolbar block-gap">
        <el-input v-model="me" placeholder="本人在群里的称呼" style="width: 180px" />
        <el-select v-model="imSource" style="width: 130px">
          <el-option label="飞书" value="feishu" />
          <el-option label="企业微信" value="wechat" />
          <el-option label="通用" value="generic" />
        </el-select>
      </div>
      <el-input v-model="messagesText" type="textarea" :rows="4" placeholder="每行一条，格式“发送人: 内容”" class="block-gap" />
      <el-button :loading="tab3Busy" @click="doDigest">生成摘要</el-button>
      <p v-if="digest?.brief" class="muted">{{ digest.brief }}</p>
      <el-table v-if="digest?.my_tasks?.length" :data="digest.my_tasks" size="small" class="block-gap">
        <el-table-column prop="task" label="@我的任务" min-width="200" show-overflow-tooltip />
        <el-table-column prop="from" label="来自" width="100" />
        <el-table-column prop="due" label="截止" width="110">
          <template #default="{ row }">{{ row.due ?? '未给出' }}</template>
        </el-table-column>
      </el-table>
      <el-alert v-if="pageError" class="block-gap" type="error" :title="pageError" show-icon :closable="false" />
    </el-card>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  gap: 12px;
  margin-bottom: 12px;
}
</style>
