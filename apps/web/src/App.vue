<script setup lang="ts">
// 职责：智能办公 Agent 前端单页（无路由库，ref 切换登录卡 / 主面板）
// 链路：App.vue → api.ts request() → 后端契约（auth/agent.tools/tasks/approvals，前缀 /api/v1）
// 面板：顶栏（标题+用户+退出）、左区工具列表、右区工具调用（含 trace 与数据溯源）、任务列表、审批列表
// 交互成品化：加载态 / 空态 / 可关闭错误条 / 驳回理由弹窗（替代 window.prompt）/ 按钮进行中禁用
import { computed, nextTick, onMounted, ref } from 'vue'
import {
  clearToken,
  getBase,
  getToken,
  request,
  setBase,
  setToken,
  setUnauthorizedHandler,
} from './api'

// ---------- 数据类型（与后端契约一一对应） ----------
interface ToolItem {
  name: string
  description: string
  scope: string
  requires_approval: boolean
  provider_id?: string
}
interface ToolList {
  total: number
  items: ToolItem[]
}
interface TaskItem {
  id: string
  type: string
  status: string
  progress: number
  created_at: string
}
interface ApprovalItem {
  id: string
  action: string
  target: string
  status: string
  status_label: string
  applicant: string
  created_at: string
}
// 跨系统数据出处（远程工具才有；由内核联动层按实测填写，前端只展示不加工）
interface Provenance {
  provider_id?: string
  source_endpoint?: string
  fetched_at?: string
  upstream_trace_id?: string
}
interface InvokeResult {
  tool: string
  status: string
  trace_id: string
  latency_ms: number
  attempts: number
  requires_approval: boolean
  approval_required: boolean
  approval_id: string
  provider_id?: string
  result?: unknown
  provenance?: Provenance
}

const USER_KEY = 'office_user'

// ---------- 登录态 ----------
const logged = ref(!!getToken()) // 已有 token 直接进主面板
const base = ref(getBase())
const username = ref('')
const password = ref('')
const currentUser = ref(localStorage.getItem(USER_KEY) ?? '')
const errBar = ref('') // 红条错误提示（登录卡与主面板共用）
const loggingIn = ref(false)
const canLogin = computed(() => !!username.value && !!password.value)

// 401 统一处理（api.ts 回调）：token 已在 api 内清除，这里切回登录卡
setUnauthorizedHandler(() => {
  logged.value = false
  errBar.value = '登录已失效，请重新登录'
})

// ---------- 列表数据加载 ----------
const tools = ref<ToolItem[]>([])
const tasks = ref<TaskItem[]>([])
const approvals = ref<ApprovalItem[]>([])
const loadingLists = ref(false) // 首屏与刷新共用：加载中显示占位文案，不闪空白

const loadTools = async () => {
  tools.value = (await request<ToolList>('/agent/tools')).items
}
const loadTasks = async () => {
  tasks.value = await request<TaskItem[]>('/tasks')
}
const loadApprovals = async () => {
  approvals.value = await request<ApprovalItem[]>('/approvals')
}
const loadAll = async () => {
  errBar.value = ''
  loadingLists.value = true
  try {
    await Promise.all([loadTools(), loadTasks(), loadApprovals()])
  } catch (e) {
    errBar.value = (e as Error).message || '列表加载失败'
  } finally {
    loadingLists.value = false
  }
}

// ---------- 登录 / 退出 ----------
const doLogin = async () => {
  if (!canLogin.value || loggingIn.value) return
  errBar.value = ''
  // 服务地址必须先落盘再发请求：request() 读的是已保存的 base，
  // 若放到成功之后再存，本次登录仍会打向默认/上次地址，输入框形同虚设
  base.value = base.value.trim().replace(/\/+$/, '')
  setBase(base.value)
  loggingIn.value = true
  try {
    const data = await request<{ token: string }>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username: username.value, password: password.value }),
    })
    setToken(data.token)
    currentUser.value = username.value
    localStorage.setItem(USER_KEY, username.value)
    password.value = '' // 口令不留在内存里的表单上
    logged.value = true
    await loadAll()
  } catch (e) {
    errBar.value = (e as Error).message || '登录失败'
  } finally {
    loggingIn.value = false
  }
}

const doLogout = () => {
  clearToken()
  localStorage.removeItem(USER_KEY)
  currentUser.value = ''
  logged.value = false
  errBar.value = ''
  tools.value = []
  tasks.value = []
  approvals.value = []
}

// ---------- 工具调用 ----------
const invokeName = ref('')
const invokeArgs = ref('{}')
const invokeData = ref<InvokeResult | null>(null) // 成功结果（含 trace 与数据出处）
const invokeError = ref('') // 失败信息（红色）
const invoking = ref(false)
const stringify = (value: unknown): string => JSON.stringify(value, null, 2)

// 点工具卡片：把工具名填进调用表单（卡片高亮选中项，避免不知道当前在调哪个）
const pickTool = (name: string) => {
  invokeName.value = name
}

const doInvoke = async () => {
  invokeData.value = null
  invokeError.value = ''
  let args: unknown
  try {
    args = JSON.parse(invokeArgs.value || '{}') // JSON.parse 必须 try/catch
  } catch {
    invokeError.value = '参数不是合法 JSON，请检查格式'
    return
  }
  invoking.value = true
  try {
    // 工具名进路径，request 统一加 /api/v1 前缀并解析 {code,msg,data,trace_id}
    const data = await request<InvokeResult>(
      `/agent/tools/${encodeURIComponent(invokeName.value)}/invoke`,
      { method: 'POST', body: JSON.stringify({ args }) },
    )
    invokeData.value = data
  } catch (e) {
    invokeError.value = (e as Error).message || '调用失败'
  } finally {
    invoking.value = false
  }
}

// ---------- 审批：批准 / 驳回 ----------
const decidingId = ref('') // 正在处理的审批单（该行按钮禁用并显示进行中）
const rejectTarget = ref<ApprovalItem | null>(null) // 驳回弹窗目标单
const rejectReason = ref('')
const rejectError = ref('')
const reasonInput = ref<HTMLTextAreaElement | null>(null)

const openReject = async (item: ApprovalItem) => {
  rejectTarget.value = item
  rejectReason.value = ''
  rejectError.value = ''
  await nextTick() // 弹窗渲染后再聚焦，键盘可直接输入理由
  reasonInput.value?.focus()
}
const closeReject = () => {
  rejectTarget.value = null
  rejectReason.value = ''
  rejectError.value = ''
}

// 审批决定唯一出口：成功才关弹窗；失败在弹窗内就地提示，不关窗丢输入
const submitDecision = async (id: string, approve: boolean, reason = '') => {
  errBar.value = ''
  decidingId.value = id
  try {
    await request(`/approvals/${encodeURIComponent(id)}/${approve ? 'approve' : 'reject'}`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
    })
    closeReject()
    // 审批决定后任务可能开始执行，两表一起刷新
    await Promise.all([loadApprovals(), loadTasks()])
  } catch (e) {
    const msg = (e as Error).message || '审批操作失败'
    if (rejectTarget.value?.id === id) rejectError.value = msg
    else errBar.value = msg
  } finally {
    decidingId.value = ''
  }
}

// 驳回理由必填（服务端 1001 拦截）：先在本地收齐，避免空理由白跑一次请求
const confirmReject = async () => {
  const reason = rejectReason.value.trim()
  if (!reason) {
    rejectError.value = '驳回理由必填，请填写后再驳回'
    return
  }
  const target = rejectTarget.value
  if (!target) return
  await submitDecision(target.id, false, reason)
}

onMounted(() => {
  // 已有 token 直接进主面板并拉取列表
  if (getToken()) loadAll()
})
</script>

<template>
  <!-- 登录卡 -->
  <div v-if="!logged" class="login-wrap">
    <div class="card login-card">
      <div class="brand">
        <span class="brand-mark">OA</span>
        <div>
          <h1 class="login-title">智能办公 Agent</h1>
          <p class="muted">工具注册 · Scope 鉴权 · 审批闸门 · 全程审计</p>
        </div>
      </div>

      <div v-if="errBar" class="err-bar">
        <span class="err-text">{{ errBar }}</span>
        <button class="err-close" title="关闭" @click="errBar = ''">×</button>
      </div>

      <label class="field">
        <span>服务地址</span>
        <input v-model="base" placeholder="http://127.0.0.1:8200" />
      </label>
      <label class="field">
        <span>用户名</span>
        <input
          v-model="username"
          placeholder="请输入用户名"
          autocomplete="username"
          @keyup.enter="doLogin"
        />
      </label>
      <label class="field">
        <span>密码</span>
        <input
          v-model="password"
          type="password"
          placeholder="请输入密码"
          autocomplete="current-password"
          @keyup.enter="doLogin"
        />
      </label>
      <button class="btn primary block" :disabled="!canLogin || loggingIn" @click="doLogin">
        {{ loggingIn ? '登录中…' : '登录' }}
      </button>
    </div>
  </div>

  <!-- 主面板 -->
  <div v-else class="panel">
    <!-- 顶栏（吸顶：长列表滚动时仍可见当前用户与退出） -->
    <header class="topbar">
      <span class="topbar-title">
        <span class="brand-mark small">OA</span>
        智能办公 Agent
      </span>
      <span class="topbar-right">
        <span class="user">{{ currentUser || '已登录' }}</span>
        <button class="btn ghost" @click="doLogout">退出</button>
      </span>
    </header>

    <div v-if="errBar" class="err-bar">
      <span class="err-text">{{ errBar }}</span>
      <button class="err-close" title="关闭" @click="errBar = ''">×</button>
    </div>

    <!-- 左右两区：工具列表 + 工具调用 -->
    <div class="grid">
      <section class="card">
        <h2>
          工具列表
          <span v-if="tools.length" class="badge">{{ tools.length }}</span>
        </h2>
        <p v-if="loadingLists" class="empty">加载中…</p>
        <div v-else-if="!tools.length" class="empty">
          暂无已注册工具
          <span class="muted">若预期有工具，请确认插件所需的上游提供方已配置</span>
        </div>
        <template v-else>
          <div
            v-for="t in tools"
            :key="t.name"
            :class="['tool-card', { active: t.name === invokeName }]"
            @click="pickTool(t.name)"
          >
            <div class="tool-head">
              <strong>{{ t.name }}</strong>
              <span class="badge">{{ t.scope }}</span>
              <span v-if="t.requires_approval" class="badge warn">需审批</span>
              <span v-if="t.provider_id" class="badge">远程 · {{ t.provider_id }}</span>
            </div>
            <p class="muted">{{ t.description }}</p>
          </div>
        </template>
      </section>

      <section class="card">
        <h2>工具调用</h2>
        <label class="field">
          <span>工具名称</span>
          <input v-model="invokeName" placeholder="点击左侧工具卡片，或直接输入工具名" />
        </label>
        <label class="field">
          <span>参数 JSON</span>
          <textarea
            v-model="invokeArgs"
            rows="5"
            spellcheck="false"
            placeholder='{"key": "value"}'
          ></textarea>
        </label>
        <button class="btn primary" :disabled="invoking || !invokeName" @click="doInvoke">
          {{ invoking ? '调用中…' : '调用' }}
        </button>

        <div v-if="invokeData" class="result ok">
          <div class="tool-head">
            <span class="badge ok">{{ invokeData.status }}</span>
            <span class="badge">耗时 {{ invokeData.latency_ms }}ms</span>
            <span class="badge">尝试 {{ invokeData.attempts }} 次</span>
            <span v-if="invokeData.approval_required" class="badge warn">已提交审批</span>
            <span class="badge trace" :title="invokeData.trace_id">
              trace {{ invokeData.trace_id }}
            </span>
          </div>
          <!-- 数据出处：跨系统取数的可见性（联动的验收面） -->
          <p v-if="invokeData.provenance" class="provenance">
            数据出处：<code>{{ invokeData.provenance.source_endpoint }}</code>
            <span class="muted">（取数时刻 {{ invokeData.provenance.fetched_at }}）</span>
          </p>
          <pre>{{ stringify(invokeData.result ?? invokeData) }}</pre>
        </div>
        <div v-if="invokeError" class="result fail">{{ invokeError }}</div>
      </section>
    </div>

    <!-- 任务列表 -->
    <section class="card block-gap">
      <h2>
        任务列表
        <span v-if="tasks.length" class="badge">{{ tasks.length }}</span>
      </h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>类型</th>
              <th>状态</th>
              <th>进度</th>
              <th>创建时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="loadingLists">
              <td colspan="5" class="empty">加载中…</td>
            </tr>
            <tr v-else-if="!tasks.length">
              <td colspan="5" class="empty">暂无任务记录</td>
            </tr>
            <template v-else>
              <tr v-for="t in tasks" :key="t.id">
                <td class="mono">{{ t.id }}</td>
                <td>{{ t.type }}</td>
                <td>
                  <span class="badge">{{ t.status }}</span>
                </td>
                <td>{{ t.progress }}%</td>
                <td class="muted">{{ t.created_at }}</td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
    </section>

    <!-- 审批列表 -->
    <section class="card">
      <h2>
        审批列表
        <span v-if="approvals.length" class="badge">{{ approvals.length }}</span>
      </h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>审批类型</th>
              <th>目标</th>
              <th>状态</th>
              <th>申请人</th>
              <th>创建时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="loadingLists">
              <td colspan="7" class="empty">加载中…</td>
            </tr>
            <tr v-else-if="!approvals.length">
              <td colspan="7" class="empty">暂无审批单</td>
            </tr>
            <template v-else>
              <tr v-for="a in approvals" :key="a.id">
                <td class="mono">{{ a.id }}</td>
                <td>{{ a.action }}</td>
                <td>{{ a.target }}</td>
                <td>
                  <span
                    class="badge"
                    :class="{ ok: a.status === 'approved', danger: a.status === 'rejected' }"
                  >
                    {{ a.status_label || a.status }}
                  </span>
                </td>
                <td>{{ a.applicant }}</td>
                <td class="muted">{{ a.created_at }}</td>
                <td class="ops">
                  <button
                    class="btn small"
                    :disabled="decidingId === a.id"
                    @click="submitDecision(a.id, true)"
                  >
                    {{ decidingId === a.id ? '处理中…' : '批准' }}
                  </button>
                  <button
                    class="btn small danger"
                    :disabled="decidingId === a.id"
                    @click="openReject(a)"
                  >
                    驳回
                  </button>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
    </section>
  </div>

  <!-- 驳回理由弹窗（替代 window.prompt：可就地看错误、不丢已输入内容） -->
  <div v-if="rejectTarget" class="modal-mask" @click.self="closeReject">
    <div class="card modal">
      <h2>驳回审批单</h2>
      <p class="muted">
        {{ rejectTarget.action }} · {{ rejectTarget.target }}
        <span class="mono">（{{ rejectTarget.id }}）</span>
      </p>
      <div v-if="rejectError" class="err-bar">
        <span class="err-text">{{ rejectError }}</span>
      </div>
      <label class="field">
        <span>驳回理由（必填）</span>
        <textarea
          ref="reasonInput"
          v-model="rejectReason"
          rows="4"
          placeholder="请说明驳回原因，将随审批结果留痕"
          @keydown.esc="closeReject"
        ></textarea>
      </label>
      <div class="modal-actions">
        <button class="btn" :disabled="!!decidingId" @click="closeReject">取消</button>
        <button class="btn danger" :disabled="!!decidingId" @click="confirmReject">
          {{ decidingId ? '提交中…' : '确认驳回' }}
        </button>
      </div>
    </div>
  </div>
</template>