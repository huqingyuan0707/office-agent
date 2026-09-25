<script setup lang="ts">
// 职责：对话主入口页（Element Plus 版）—— 员工一句话直达（PRD §4.3 纯自然语言零门槛）：
//       输入目标 → POST /runs 省略 agent 自动路由（后端挑智能体）→ 2s 轮询 run 详情 →
//       气泡内呈现：路由到的智能体、步骤时间线、终答/末步结果、审批挂起提示（链去审批页）；
//       末步结果是 markdown 文档产物（日报/纪要等）时渲染为固定格式文档卡（不摊参数），
//       「下载 Word」经 office.docx.render 真接口取 base64 还原 Blob 客户端落盘。
// 链路：router /chat → api.createRunAuto / api.getRun（真实接口零 mock：路由不中/调用失败
//       均以服务端中文 msg 如实进气泡，失败置空不编造）；onUnmounted 清全部轮询定时器。
// 对齐：AGENTS.md §4 前端红线（401 中央处理、箭头函数、var(--*) token + scoped）；
//       .trae/documents/智能体编排层实现方案.md §4（POST /runs agent 可选即自动路由）。
import { inject, nextTick, onUnmounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { Download, Promotion, Service } from '@element-plus/icons-vue'
import { createRunAuto, getRun, invokeTool } from '../api'
import type { RunItem } from '../api'
import StatusBadge from '../components/StatusBadge.vue'

const shell = inject('shellError') as {
  showError: (msg: string) => unknown
  clearError: () => unknown
}

// 单条消息：用户气泡存 text；助手侧存 run（轮询更新）或 error（路由失败等）
interface ChatMsg {
  key: number
  role: 'user' | 'agent'
  text?: string
  error?: string
  run?: RunItem
  polling?: boolean
}

const messages = ref<ChatMsg[]>([])
const input = ref('')
const sending = ref(false)
const composing = ref(false) // 中文输入法组词中：Enter 只上屏不发送
// 结构性类型收窄即可（只用到 setScrollTop），滚动到底无需拿 DOM
const listRef = ref<{ setScrollTop: (top: number) => unknown } | null>(null)
let seq = 0
const timers = new Set<ReturnType<typeof setInterval>>()

// 终态集合：与智能体页同口径，轮询命中即停。run.status 是后端大写枚举（DONE/FAILED）——转小写比对
const TERMINAL_STATUSES = ['succeeded', 'failed', 'cancelled', 'completed', 'done']
const isTerminal = (status: string) => TERMINAL_STATUSES.includes((status || '').toLowerCase())

const scrollBottom = () => {
  nextTick(() => listRef.value?.setScrollTop(1e6))
}

const pushMsg = (msg: Omit<ChatMsg, 'key'>) => {
  const full = { ...msg, key: ++seq } as ChatMsg
  messages.value.push(full)
  scrollBottom()
  // 返回数组里的响应式代理：push 存的是代理，直接改 raw 对象会绕过依赖收集，
  // 后续 msg.run / msg.error 赋值必须经代理才可靠触发重渲染（错误气泡不再卡在“挑选”态）
  return messages.value[messages.value.length - 1] as ChatMsg
}

const stopPoll = (timer: ReturnType<typeof setInterval>) => {
  clearInterval(timer)
  timers.delete(timer)
}

const pollRun = (msg: ChatMsg) => {
  msg.polling = true
  const timer = setInterval(async () => {
    if (!msg.run) return stopPoll(timer)
    try {
      const view = await getRun(msg.run.run_id)
      msg.run = view
      scrollBottom()
      if (isTerminal(view.status)) {
        msg.polling = false
        stopPoll(timer)
      }
    } catch (e) {
      msg.polling = false
      msg.error = (e as Error).message || '运行状态刷新失败'
      stopPoll(timer)
    }
  }, 2000)
  timers.add(timer)
}

const send = async (rawGoal?: string) => {
  const goal = (typeof rawGoal === 'string' ? rawGoal : input.value).trim()
  if (!goal || sending.value) return
  input.value = ''
  sending.value = true
  shell.clearError()
  pushMsg({ role: 'user', text: goal })
  const msg = pushMsg({ role: 'agent' })
  try {
    msg.run = await createRunAuto(goal)
    scrollBottom()
    pollRun(msg)
  } catch (e) {
    msg.error = (e as Error).message || '发起运行失败'
  } finally {
    sending.value = false
  }
}

// Enter 发送 / Shift+Enter 换行 / 输入法组词中不发送
const onKeydown = (e: KeyboardEvent) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    if (composing.value) return
    void send()
  }
}

// 时间线节点类型：终态失败红、挂起黄、成功绿、其余灰
const stepType = (status: string) => {
  if (['failed', 'cancelled', 'error'].includes(status)) return 'danger'
  if (['pending', 'running'].includes(status)) return 'warning'
  if (['succeeded', 'completed', 'done'].includes(status)) return 'success'
  return 'primary'
}

const argsBrief = (args: unknown) => {
  const text = JSON.stringify(args ?? {})
  return text.length > 70 ? `${text.slice(0, 70)}…` : text
}

// 示例句点击即执行：终答里「…」引住的短语（2-24 字、不含换行）拆成独立标签渲染
const EXAMPLE_RE = /「[^「」\n]{2,24}」/g

const answerSegments = (text: string) => {
  const segs: { text: string; example: boolean }[] = []
  let last = 0
  for (const match of text.matchAll(EXAMPLE_RE)) {
    const at = match.index ?? 0
    if (at > last) segs.push({ text: text.slice(last, at), example: false })
    segs.push({ text: match[0].slice(1, -1), example: true })
    last = at + match[0].length
  }
  if (last < text.length) segs.push({ text: text.slice(last), example: false })
  return segs
}

const quickSend = (goal: string) => {
  if (sending.value || !goal.trim()) return
  void send(goal)
}

// ---------- 文档卡：末步结果里 markdown 产物按固定格式渲染（PRD §2.1 一键导出 Word） ----------
interface RunDoc {
  title: string
  markdown: string
}

// 文档字段口径：办公工具 markdown 产物的常见键（命中即整块按文档渲染，其余参数不摊开）
const DOC_FIELDS = ['report', 'worklog', 'minutes', 'agenda', 'content', 'summary', 'markdown']

const runDoc = (run: RunItem): RunDoc | null => {
  const steps = run.steps ?? []
  for (let i = steps.length - 1; i >= 0; i -= 1) {
    const result = steps[i].result
    if (!result || typeof result !== 'object') continue
    const bag = result as Record<string, unknown>
    for (const key of DOC_FIELDS) {
      const value = bag[key]
      if (typeof value !== 'string' || value.length < 40 || !value.includes('\n')) continue
      const title =
        (typeof bag.title === 'string' && bag.title.trim()) ||
        (/^#\s+(.+)$/m.exec(value)?.[1] ?? '').trim() ||
        run.goal
      // 首行「# 标题」与文档标题重复时剥掉（卡片头已展示标题，正文不重复）
      const lines = value.split('\n')
      const markdown =
        lines[0]?.trim() === `# ${title}` ? lines.slice(1).join('\n').replace(/^\n+/, '') : value
      return { title, markdown }
    }
  }
  return null
}

const escapeHtml = (s: string) =>
  s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

// 行内只认 **加粗**，其余先转义再替换（自家渲染器零依赖零 XSS 面）
const mdInline = (s: string) => escapeHtml(s).replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')

// markdown 子集渲染：# 标题 / - 与 1. 列表 / | 表格 | / > 引用 / --- 分隔线 / **加粗**
const renderMarkdown = (md: string): string => {
  const out: string[] = []
  let list: 'ul' | 'ol' | null = null
  let table: string[][] = []
  const closeList = () => {
    if (list) {
      out.push(`</${list}>`)
      list = null
    }
  }
  const flushTable = () => {
    const rows = table.filter((r) => !r.every((c) => /^:?-{2,}:?$/.test(c)))
    if (rows.length) {
      const head = rows[0] ?? []
      const body = rows.slice(1)
      out.push(
        `<table><thead><tr>${head.map((c) => `<th>${mdInline(c)}</th>`).join('')}</tr></thead>`,
        body.length
          ? `<tbody>${body.map((r) => `<tr>${r.map((c) => `<td>${mdInline(c)}</td>`).join('')}</tr>`).join('')}</tbody>`
          : '',
        '</table>',
      )
    }
    table = []
  }
  for (const raw of md.split(/\r?\n/)) {
    const line = raw.trim()
    if (line.startsWith('|')) {
      closeList()
      table.push(line.replace(/^\||\|$/g, '').split('|').map((c) => c.trim()))
      continue
    }
    flushTable()
    if (!line) {
      closeList()
      continue
    }
    const h = /^(#{1,4})\s+(.*)$/.exec(line)
    if (h) {
      closeList()
      const lv = Math.min(h[1].length + 1, 6)
      out.push(`<h${lv}>${mdInline(h[2])}</h${lv}>`)
      continue
    }
    if (/^(-{3,}|\*{3,})$/.test(line)) {
      closeList()
      out.push('<hr/>')
      continue
    }
    const bullet = /^[-*•]\s+(.*)$/.exec(line)
    if (bullet) {
      if (list !== 'ul') {
        closeList()
        out.push('<ul>')
        list = 'ul'
      }
      out.push(`<li>${mdInline(bullet[1])}</li>`)
      continue
    }
    const ordered = /^\d+[.、)]\s*(.*)$/.exec(line)
    if (ordered) {
      if (list !== 'ol') {
        closeList()
        out.push('<ol>')
        list = 'ol'
      }
      out.push(`<li>${mdInline(ordered[1])}</li>`)
      continue
    }
    const quote = /^>\s?(.*)$/.exec(line)
    if (quote) {
      closeList()
      out.push(`<blockquote>${mdInline(quote[1])}</blockquote>`)
      continue
    }
    closeList()
    out.push(`<p>${mdInline(line)}</p>`)
  }
  closeList()
  flushTable()
  return out.join('')
}

// 下载 Word：office.docx.render 真接口（读免审）→ base64 还原 Blob 客户端落盘，零 mock
const downloadingKey = ref(0)

const downloadDocx = async (msg: ChatMsg) => {
  const doc = msg.run ? runDoc(msg.run) : null
  if (!doc || downloadingKey.value) return
  downloadingKey.value = msg.key
  try {
    const res = await invokeTool('office.docx.render', { title: doc.title, markdown: doc.markdown })
    const payload = res.result as { content?: string; filename?: string } | undefined
    if (!payload?.content) throw new Error('服务端未返回 Word 内容，请稍后重试')
    const bytes = Uint8Array.from(atob(payload.content), (c) => c.charCodeAt(0))
    const url = URL.createObjectURL(
      new Blob([bytes], {
        type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      }),
    )
    const a = document.createElement('a')
    a.href = url
    a.download = payload.filename || `${doc.title}.docx`
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) {
    shell.showError((e as Error).message || '下载 Word 失败')
  } finally {
    downloadingKey.value = 0
  }
}

// 末步结果：规则智能体常无 LLM 终答，末步真实出参即「办的结果」（如实渲染，无则不显）
const lastResultText = (run: RunItem) => {
  const steps = run.steps ?? []
  for (let i = steps.length - 1; i >= 0; i -= 1) {
    const result = (steps[i] as { result?: unknown }).result
    if (result !== undefined && result !== null) {
      const text = JSON.stringify(result, null, 2)
      return text.length > 600 ? `${text.slice(0, 600)}…` : text
    }
  }
  return ''
}

onUnmounted(() => {
  timers.forEach((timer) => clearInterval(timer))
  timers.clear()
})
</script>

<template>
  <div class="chat-page">
    <el-card class="chat-card" shadow="never">
      <template #header>
        <div class="chat-head">
          <span class="card-title">对话办理</span>
          <span class="muted head-hint">
            用一句话说明要办的事，系统自动挑选智能体执行；涉及审批的写动作会挂起待复核员批准
          </span>
        </div>
      </template>

      <el-scrollbar ref="listRef" class="msg-list">
        <el-empty
          v-if="!messages.length"
          :image-size="86"
          description="还没有对话。试试：「生成今天的工作日报」「记一下明天要跟进的事」"
        />

        <template v-for="m in messages" :key="m.key">
          <!-- 用户气泡：右对齐，品牌色实底 -->
          <div v-if="m.role === 'user'" class="row user-row">
            <div class="bubble user-bubble">{{ m.text }}</div>
          </div>

          <!-- 助手气泡：左对齐，头像 + 白底卡 -->
          <div v-else class="row agent-row">
            <el-avatar :size="30" class="agent-avatar">
              <el-icon><Service /></el-icon>
            </el-avatar>
            <div class="bubble agent-bubble">
              <el-alert v-if="m.error" type="error" :title="m.error" show-icon :closable="false" />

              <template v-else-if="m.run">
                <div class="run-head">
                  <el-tag size="small" type="primary" effect="dark" round>
                    {{ m.run.agent || '智能体' }}
                  </el-tag>
                  <StatusBadge :status="m.run.status" />
                  <el-tag v-if="m.polling" size="small" type="warning" effect="light" round>
                    执行中…
                  </el-tag>
                </div>

                <el-timeline v-if="m.run.steps?.length" class="steps">
                  <el-timeline-item
                    v-for="s in m.run.steps"
                    :key="s.step_index"
                    :type="stepType(s.status)"
                    size="normal"
                    hollow
                  >
                    <div class="step-line">
                      <span class="step-no">步骤 {{ s.step_index }}</span>
                      <strong class="mono">{{ s.tool }}</strong>
                      <StatusBadge :status="s.status" />
                    </div>
                    <el-tooltip :content="JSON.stringify(s.args)" placement="top">
                      <p class="muted mono args">{{ argsBrief(s.args) }}</p>
                    </el-tooltip>
                  </el-timeline-item>
                </el-timeline>

                <pre v-if="m.run.answer" class="answer"><!-- 终答里的「…」示例短语渲染为可点击标签，一键发起 -->
<template v-for="(seg, si) in answerSegments(m.run.answer)" :key="si"><el-tag
                    v-if="seg.example"
                    class="example-tag"
                    size="small"
                    type="primary"
                    effect="light"
                    round
                    @click="quickSend(seg.text)"
                  >{{ seg.text }}</el-tag><span v-else>{{ seg.text }}</span></template
                ></pre>
                <!-- 末步结果是 markdown 文档产物：固定格式文档卡渲染（不摊参数）+ 下载 Word -->
                <div v-else-if="runDoc(m.run)" class="doc-card">
                  <div class="doc-head">
                    <span class="doc-title">{{ runDoc(m.run)!.title }}</span>
                    <el-button
                      size="small"
                      type="primary"
                      :icon="Download"
                      :loading="downloadingKey === m.key"
                      @click="downloadDocx(m)"
                    >
                      下载 Word
                    </el-button>
                  </div>
                  <!-- 自家子集渲染器先转义后替换，无第三方 HTML 注入面 -->
                  <div class="doc-body" v-html="renderMarkdown(runDoc(m.run)!.markdown)"></div>
                </div>
                <pre v-else-if="lastResultText(m.run)" class="answer">{{
                  lastResultText(m.run)
                }}</pre>

                <el-alert
                  v-if="m.run.pending_approval"
                  class="pending-box"
                  type="warning"
                  show-icon
                  :closable="false"
                  title="存在待审批写动作，复核员批准后自动继续"
                >
                  <RouterLink class="alert-link" to="/approvals">前往审批页 →</RouterLink>
                </el-alert>
                <p v-if="m.run.approval_hint" class="muted">{{ m.run.approval_hint }}</p>
                <el-alert
                  v-if="m.run.error"
                  type="error"
                  :title="m.run.error"
                  show-icon
                  :closable="false"
                />
              </template>

              <span v-else class="muted waiting">正在挑选智能体…</span>
            </div>
          </div>
        </template>
      </el-scrollbar>

      <div class="composer">
        <el-input
          v-model="input"
          type="textarea"
          :rows="2"
          resize="none"
          placeholder="用一句话说明要办的事，Enter 发送（Shift+Enter 换行）"
          @keydown="onKeydown"
          @compositionstart="composing = true"
          @compositionend="composing = false"
        />
        <el-button
          type="primary"
          :icon="Promotion"
          :loading="sending"
          :disabled="!input.trim()"
          @click="send()"
        >
          发送
        </el-button>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.chat-page {
  display: flex;
  justify-content: center;
}
.chat-card {
  width: min(880px, 100%);
}
.chat-head {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.msg-list {
  height: 56vh;
  min-height: 320px;
  padding-right: 6px;
}
.row {
  display: flex;
  margin: 12px 0;
  gap: 10px;
}
.user-row {
  justify-content: flex-end;
}
.agent-row {
  justify-content: flex-start;
}
.agent-avatar {
  flex: none;
  background: var(--brand-soft);
  color: var(--brand);
}
.bubble {
  max-width: 82%;
  border-radius: 12px;
  padding: 10px 14px;
  white-space: pre-wrap;
  word-break: break-word;
}
.user-bubble {
  background: linear-gradient(135deg, var(--brand), #4f8ff5);
  color: #fff;
  box-shadow: 0 2px 8px rgba(31, 111, 235, 0.22);
}
.agent-bubble {
  flex: 1;
  min-width: 0;
  background: #fff;
  border: 1px solid var(--line);
  box-shadow: var(--shadow-card);
}
.run-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.steps {
  margin-top: 10px;
  padding-left: 2px;
}
.steps :deep(.el-timeline-item__wrapper) {
  padding-left: 22px;
}
.steps :deep(.el-timeline-item) {
  padding-bottom: 10px;
}
.step-line {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 13px;
}
.step-no {
  color: var(--muted);
  font-size: 12px;
}
.args {
  font-size: 12px;
  margin: 2px 0 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.answer {
  margin: 10px 0 0;
  padding: 10px 12px;
  background: var(--ok-bg);
  border: 1px solid #cfe8d5;
  border-radius: var(--radius-sm);
  font-size: 13px;
  max-height: 220px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
.doc-card {
  margin: 10px 0 0;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  overflow: hidden;
  white-space: normal;
}
.doc-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 8px 14px;
  background: var(--brand-soft);
  border-bottom: 1px solid var(--line);
}
.doc-title {
  font-weight: 700;
  font-size: 14px;
  color: var(--brand);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.doc-body {
  padding: 12px 16px;
  background: #fff;
  font-size: 13px;
  line-height: 1.75;
  max-height: 340px;
  overflow: auto;
  word-break: break-word;
}
.doc-body :deep(h2),
.doc-body :deep(h3),
.doc-body :deep(h4) {
  margin: 12px 0 6px;
  font-size: 14px;
  color: var(--fg);
}
.doc-body :deep(h2) {
  font-size: 15px;
  padding-bottom: 4px;
  border-bottom: 1px solid var(--line);
}
.doc-body :deep(p) {
  margin: 6px 0;
}
.doc-body :deep(ul),
.doc-body :deep(ol) {
  margin: 6px 0;
  padding-left: 22px;
}
.doc-body :deep(li) {
  margin: 2px 0;
}
.doc-body :deep(table) {
  margin: 8px 0;
  border-collapse: collapse;
  width: 100%;
  font-size: 12px;
}
.doc-body :deep(th),
.doc-body :deep(td) {
  border: 1px solid var(--line);
  padding: 5px 8px;
  text-align: left;
}
.doc-body :deep(th) {
  background: var(--brand-soft);
}
.doc-body :deep(blockquote) {
  margin: 8px 0;
  padding: 4px 10px;
  border-left: 3px solid var(--brand);
  background: var(--brand-soft);
  color: var(--muted);
}
.doc-body :deep(hr) {
  margin: 10px 0;
  border: none;
  border-top: 1px dashed var(--line);
}
.pending-box {
  margin-top: 10px;
}
.example-tag {
  cursor: pointer;
  margin: 0 3px;
  vertical-align: baseline;
}
.example-tag:hover {
  background: var(--brand-soft);
  border-color: var(--brand);
  color: var(--brand);
}
.alert-link {
  color: var(--warn);
  font-weight: 600;
}
.waiting {
  font-size: 13px;
}
.composer {
  display: flex;
  gap: 10px;
  align-items: flex-end;
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px solid var(--line);
}
</style>