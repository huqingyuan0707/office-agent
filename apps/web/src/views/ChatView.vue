<script setup lang="ts">
// 职责：对话主入口页 —— 员工一句话直达（PRD §4.3 纯自然语言零门槛）：输入目标 →
//       POST /runs 省略 agent 自动路由（后端挑智能体）→ 2s 轮询 run 详情 →
//       气泡内呈现：路由到的智能体、步骤时间线、终答/末步结果、审批挂起卡（链去审批页）。
// 链路：router /chat → api.createRunAuto / api.getRun（真实接口零 mock：路由不中/调用失败
//       均以服务端中文 msg 如实进气泡，失败置空不编造）；onUnmounted 清全部轮询定时器。
// 对齐：AGENTS.md §4 前端红线（401 中央处理、箭头函数、var(--*) token + scoped）；
//       .trae/documents/智能体编排层实现方案.md §4（POST /runs agent 可选即自动路由）。
import { inject, nextTick, onUnmounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { createRunAuto, getRun } from '../api'
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
const listEl = ref<HTMLElement | null>(null)
let seq = 0
const timers = new Set<ReturnType<typeof setInterval>>()

// 终态集合：与智能体页同口径，轮询命中即停
const TERMINAL_STATUSES = ['succeeded', 'failed', 'cancelled', 'completed']

const scrollBottom = () => {
  nextTick(() => {
    if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight
  })
}

const pushMsg = (msg: Omit<ChatMsg, 'key'>) => {
  const full = { ...msg, key: ++seq } as ChatMsg
  messages.value.push(full)
  scrollBottom()
  return full
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
      if (TERMINAL_STATUSES.includes(view.status)) {
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

const send = async () => {
  const goal = input.value.trim()
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

// Enter 发送 / Shift+Enter 换行
const onKeydown = (e: KeyboardEvent) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    void send()
  }
}

const argsBrief = (args: unknown) => {
  const text = JSON.stringify(args ?? {})
  return text.length > 60 ? `${text.slice(0, 60)}…` : text
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
    <section class="card chat-card">
      <h2>对话办理</h2>
      <p class="muted hint">
        用一句话说明要办的事，系统自动挑选智能体执行；涉及审批的写动作会挂起待复核员批准。
      </p>

      <div ref="listEl" class="msg-list">
        <p v-if="!messages.length" class="empty">
          还没有对话。试试：「生成今天的工作日报」「记一下明天要跟进的事」。
        </p>

        <template v-for="m in messages" :key="m.key">
          <div v-if="m.role === 'user'" class="row user-row">
            <div class="bubble user-bubble">{{ m.text }}</div>
          </div>

          <div v-else class="row agent-row">
            <div class="bubble agent-bubble">
              <div v-if="m.error" class="result fail">{{ m.error }}</div>

              <template v-else-if="m.run">
                <div class="run-head">
                  <span class="badge">{{ m.run.agent || '智能体' }}</span>
                  <StatusBadge :status="m.run.status" />
                  <span v-if="m.polling" class="badge warn">执行中…</span>
                </div>

                <div v-if="m.run.steps?.length" class="steps">
                  <div v-for="s in m.run.steps" :key="s.step_index" class="step-line">
                    <span class="badge">步骤 {{ s.step_index }}</span>
                    <strong class="mono">{{ s.tool }}</strong>
                    <StatusBadge :status="s.status" />
                    <span class="muted mono args" :title="JSON.stringify(s.args)">
                      {{ argsBrief(s.args) }}
                    </span>
                  </div>
                </div>

                <pre v-if="m.run.answer" class="answer">{{ m.run.answer }}</pre>
                <pre v-else-if="lastResultText(m.run)" class="answer">{{
                  lastResultText(m.run)
                }}</pre>

                <div v-if="m.run.pending_approval" class="pending-box">
                  <span class="pending-text">存在待审批写动作，复核员批准后自动继续</span>
                  <RouterLink class="err-link" to="/approvals">前往审批页 →</RouterLink>
                </div>
                <p v-if="m.run.approval_hint" class="muted">{{ m.run.approval_hint }}</p>
                <p v-if="m.run.error" class="result fail">{{ m.run.error }}</p>
              </template>

              <p v-else class="empty">正在挑选智能体…</p>
            </div>
          </div>
        </template>
      </div>

      <div class="composer">
        <textarea
          v-model="input"
          rows="2"
          placeholder="用一句话说明要办的事，Enter 发送（Shift+Enter 换行）"
          @keydown="onKeydown"
        ></textarea>
        <button class="btn primary" :disabled="sending || !input.trim()" @click="send">
          发送
        </button>
      </div>
    </section>
  </div>
</template>

<style scoped>
.chat-page {
  display: flex;
  justify-content: center;
}
.chat-card {
  width: min(860px, 100%);
  display: flex;
  flex-direction: column;
}
.hint {
  margin-top: -4px;
}
.msg-list {
  min-height: 320px;
  max-height: 56vh;
  overflow-y: auto;
  padding: 4px 2px;
}
.row {
  display: flex;
  margin: 10px 0;
}
.user-row {
  justify-content: flex-end;
}
.agent-row {
  justify-content: flex-start;
}
.bubble {
  max-width: 82%;
  border-radius: 10px;
  padding: 10px 12px;
  white-space: pre-wrap;
  word-break: break-word;
}
.user-bubble {
  background: var(--brand);
  color: #fff;
}
.agent-bubble {
  background: var(--bg);
  border: 1px solid var(--line);
}
.run-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.steps {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.step-line {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 13px;
}
.args {
  font-size: 12px;
}
.answer {
  margin: 8px 0 0;
  padding: 8px 10px;
  background: var(--ok-bg);
  border: 1px solid var(--ok-line);
  border-radius: 8px;
  font-size: 13px;
  max-height: 220px;
  overflow: auto;
}
.pending-box {
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: 10px;
  background: var(--warn-bg);
  border: 1px solid var(--warn-line);
  border-radius: 8px;
  padding: 8px 10px;
}
.pending-text {
  color: var(--warn);
  font-size: 13px;
}
.composer {
  display: flex;
  gap: 8px;
  align-items: flex-end;
  margin-top: 10px;
}
.composer textarea {
  flex: 1;
  resize: vertical;
}
</style>
