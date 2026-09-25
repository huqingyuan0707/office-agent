// 职责：后端 API 唯一入口（token/base/用户名存取、Authorization 注入、统一信封解析、401 中央处理回调）
// 链路：各视图 → 本文件具名函数 → request() → fetch(base + /api/v1 + path) → 解析 {code,msg,data,trace_id} 信封
// 契约：登录 POST /auth/login；工具 GET /agent/tools、POST /agent/tools/{name}/invoke；任务 GET /tasks；
//      审批 GET /approvals、POST /approvals/{id}/approve|reject；智能体 GET /agents（{total,items}）；
//      运行 POST /runs（{agent?, goal}——agent 省略 = 按目标自动路由）、GET /runs/{run_id}；
//      治理 GET /governance/status；运营 GET /admin/overview（admin 可见）
// 对齐：AGENTS.md §3 信封与溯源口径 + §4 前端红线（401 中央处理，视图内不自跳、禁直写 fetch）

const TOKEN_KEY = 'office_token'
const BASE_KEY = 'office_base'
const USER_KEY = 'office_user'
const DEFAULT_BASE = 'http://127.0.0.1:8200'
const API_PREFIX = '/api/v1'

// 统一信封（与后端 responses.ok()/fail() 同口径：code==0 才算成功）
interface Envelope<T> {
  code: number
  msg: string
  data: T
  trace_id: string
}

// 错误码中文兜底（服务端 msg 优先；无 msg 时按码给可读文案，前端不裸抛「code 4006」）
const CODE_MESSAGES = {
  1001: '请求参数有误，请检查后重试',
  1002: '登录已失效，请重新登录',
  1003: '权限不足，请联系管理员',
  1004: '资源不存在或无权访问',
  4005: '工具不存在，请刷新工具列表',
  4006: '缺少该工具的调用权限',
  4007: '该工具连续失败已熔断，请稍后重试',
  4008: '工具调用失败，请稍后重试',
  5000: '系统繁忙，请稍后重试',
  5001: '上游系统不可用，请稍后重试',
} as const

// ---------- 数据类型（与后端契约一一对应，视图统一从这里 import） ----------
export interface ToolItem {
  name: string
  description: string
  scope: string
  requires_approval: boolean
  provider_id?: string
}
export interface ToolList {
  total: number
  items: ToolItem[]
}
export interface TaskItem {
  id: string
  type: string
  status: string
  progress: number
  created_at: string
}
export interface ApprovalItem {
  id: string
  action: string
  target: string
  // 申请内容（送审时的工具入参）：复核员据此判断批什么，前端只展示不加工
  args?: unknown
  status: string
  status_label: string
  applicant: string
  created_at: string
}
// 跨系统数据出处（远程工具才有；由内核联动层按实测填写，前端只展示不加工）
export interface Provenance {
  provider_id?: string
  source_endpoint?: string
  fetched_at?: string
  upstream_trace_id?: string
}
export interface InvokeResult {
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
// 智能体与运行（字段可选：视图按后端实际返回渲染，缺失不编造；后端契约字段名以实测为准）
export interface AgentItem {
  name: string
  description: string
  tools?: string[]
  max_steps?: number
  llm?: string
  rule_count?: number
}
export interface AgentList {
  total: number
  items: AgentItem[]
}
export interface RunStep {
  step_index: number
  tool: string
  status: string
  planner_source?: string
  args?: unknown
  result?: unknown
  approval_id?: string
  trace_id?: string
  created_at?: string
}
export interface RunPendingApproval {
  approval_id?: string
  tool?: string
}
export interface RunItem {
  run_id: string
  agent: string
  goal: string
  username?: string
  status: string
  status_label?: string
  progress?: number
  error?: string
  next_step?: number
  created_at?: string
  steps?: RunStep[]
  answer?: string
  validation?: unknown
  pending_approval?: RunPendingApproval
  approval_hint?: string
}
// 治理状态（全部可选：缺失字段不渲染、不编造）
export interface GovernanceProvider {
  provider_id: string
  endpoint: string
}
export interface GovernanceStatus {
  app?: string
  version?: string
  env?: string
  tools?: { total?: number; remote?: number; local?: number }
  providers?: GovernanceProvider[]
  providers_missing?: string[]
  pending_approvals?: number
  audit_recent?: number
  metrics?: {
    enabled?: boolean
    dir?: string
    counters?: { [key: string]: number }
    llm_ok_rate?: number | null
    tool_ok_rate?: number | null
    linkage_ok_rate?: number | null
  }
}

// ---------- 凭据与配置存取 ----------
export const getToken = () => localStorage.getItem(TOKEN_KEY) ?? ''
export const setToken = (token: string) => localStorage.setItem(TOKEN_KEY, token)
export const clearToken = () => localStorage.removeItem(TOKEN_KEY)
export const getBase = () => localStorage.getItem(BASE_KEY) ?? DEFAULT_BASE
export const setBase = (base: string) => localStorage.setItem(BASE_KEY, base)
export const getUser = () => localStorage.getItem(USER_KEY) ?? ''
export const setUser = (name: string) => localStorage.setItem(USER_KEY, name)
export const clearUser = () => localStorage.removeItem(USER_KEY)

// 401 回调：由 main.ts 注册到 router（会话失效 → 跳登录页），视图内不做二次跳转
let unauthorizedHandler: (() => unknown) | null = null
export const setUnauthorizedHandler = (fn: () => unknown) => {
  unauthorizedHandler = fn
}

// 统一请求：自动带 Authorization 头；code!==0 抛错（服务端 msg 优先，缺省走中文错误表）；
// HTTP 401 或 code 1002 视为会话失效 → 清 token 并回调（登录接口本身无 token，不受影响）
export const request = async <T = unknown>(path: string, options: RequestInit = {}) => {
  const base = getBase().replace(/\/+$/, '')
  const headers: { [k: string]: string } = {}
  if (options.headers) Object.assign(headers, options.headers)
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  if (options.body) headers['Content-Type'] = 'application/json'

  let res: Response
  try {
    res = await fetch(base + API_PREFIX + path, { ...options, headers })
  } catch {
    throw new Error('无法连接服务，请检查服务地址是否正确')
  }

  const envelope = (await res.json().catch(() => null)) as Envelope<T> | null
  const code = envelope?.code ?? -1
  if ((res.status === 401 || code === 1002) && getToken()) {
    clearToken()
    unauthorizedHandler?.()
    throw new Error(envelope?.msg || CODE_MESSAGES[1002])
  }
  if (!envelope || typeof envelope.code !== 'number') {
    throw new Error(`请求失败（HTTP ${res.status}）`)
  }
  if (code !== 0) {
    throw new Error(
      envelope.msg || CODE_MESSAGES[code as keyof typeof CODE_MESSAGES] || `请求失败（code ${code}）`,
    )
  }
  return envelope.data as T
}

// ---------- 具名 API（视图只准走这里，禁止直写 fetch） ----------
export const login = async (username: string, password: string) =>
  request<{ token: string }>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })

export const listTools = () => request<ToolList>('/agent/tools')

export const invokeTool = (name: string, args: unknown) =>
  request<InvokeResult>(`/agent/tools/${encodeURIComponent(name)}/invoke`, {
    method: 'POST',
    body: JSON.stringify({ args }),
  })

export const listTasks = () => request<TaskItem[]>('/tasks')

export const listApprovals = () => request<ApprovalItem[]>('/approvals')

// 审批决定唯一出口：approve / reject 同体 {reason}，幂等与送审由后端审批闸门把关
export const decideApproval = (id: string, action: 'approve' | 'reject', reason = '') =>
  request<unknown>(`/approvals/${encodeURIComponent(id)}/${action}`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })

export const listAgents = () => request<AgentList>('/agents')

// 发起运行：agent 显式指定 = 直达该智能体；省略（传空串）= 一句话自动路由（后端挑智能体）
export const createRun = (agent: string, goal: string) =>
  request<RunItem>('/runs', {
    method: 'POST',
    body: JSON.stringify({ agent, goal }),
  })

export const createRunAuto = (goal: string) => createRun('', goal)

export const getRun = (runId: string) => request<RunItem>(`/runs/${encodeURIComponent(runId)}`)

export const governanceStatus = () => request<GovernanceStatus>('/governance/status')

// 运营总览（GET /admin/overview：仅 admin 可见；非 admin 403，前端如实提示不造数据；
// Top 榜与最近裁决只做计数与已决单聚合，名单明细不出聚合口）
export interface AdminOverview {
  users: { total: number; active: number; frozen: number }
  tasks: { [key: string]: number }
  approvals: { [key: string]: number }
  tool_calls: { total: number; top_tools: { name: string; count: number }[] }
  recent_decisions: {
    id: string
    action: string
    applicant: string
    approver: string
    status: string
    decided_at: string
  }[]
}

export const adminOverview = () => request<AdminOverview>('/admin/overview')
// 工作流编排（定义 CRUD + 顺序执行；步骤引用 registry 工具名，执行走同一条内核链，
// 写步骤落单即停 pending_approval；远程工具步骤天然可编排，出站溯源随步返回）
export interface WorkflowStep {
  tool: string
  args: object
}
export interface WorkflowItem {
  id: string
  name: string
  description: string
  step_count: number
  created_by: string
  updated_at: string
}
export interface WorkflowDetail extends WorkflowItem {
  steps: WorkflowStep[]
}
export interface WorkflowRunStep {
  index: number
  tool: string
  status: string
  latency_ms?: number
  approval_id?: string
  provider_id?: string
  result?: unknown
}
export interface WorkflowRun {
  workflow_id: string
  workflow_name: string
  status: string
  approval_id?: string
  next_step?: number
  error?: string
  failed_step?: number
  steps: WorkflowRunStep[]
  trace_id: string
}

export const listWorkflows = () => request<WorkflowItem[]>('/workflows')

export const createWorkflow = (name: string, description: string, steps: WorkflowStep[]) =>
  request<WorkflowDetail>('/workflows', {
    method: 'POST',
    body: JSON.stringify({ name, description, steps }),
  })

export const getWorkflow = (id: string) =>
  request<WorkflowDetail>(`/workflows/${encodeURIComponent(id)}`)

export const updateWorkflow = (
  id: string,
  patch: { name?: string; description?: string; steps?: WorkflowStep[] },
) =>
  request<WorkflowDetail>(`/workflows/${encodeURIComponent(id)}`, {
    method: 'PUT',
    body: JSON.stringify(patch),
  })

export const deleteWorkflow = (id: string) =>
  request<{ deleted: string }>(`/workflows/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  })

export const runWorkflow = (id: string) =>
  request<WorkflowRun>(`/workflows/${encodeURIComponent(id)}/run`, { method: 'POST' })

// 定时任务（PRD §2.11：作业 CRUD + 立即执行；到点由内置调度环或 /jobs/tick 手动扫描触发，
// 执行身份=创建人实时角色，workflow_run 的审批闸门在执行链里绕不过）
export interface JobSchedule {
  kind: 'daily' | 'weekly' | 'interval'
  at?: string
  day?: number
  seconds?: number
}
export interface JobItem {
  id: string
  name: string
  job_type: string
  schedule: JobSchedule
  payload: Record<string, unknown>
  enabled: boolean
  next_run_at: string
  last_run_at: string
  last_result: string
  created_by: string
}
export interface JobRunResult {
  job_id: string
  name: string
  status: string
  created?: number
  steps_done?: number
  error?: string
  [key: string]: unknown
}

export const listJobs = () => request<JobItem[]>('/jobs')

export const createJob = (
  name: string,
  jobType: string,
  schedule: JobSchedule,
  payload: Record<string, unknown>
) =>
  request<JobItem>('/jobs', {
    method: 'POST',
    body: JSON.stringify({ name, job_type: jobType, schedule, payload }),
  })

export const updateJob = (
  id: string,
  patch: { name?: string; schedule?: JobSchedule; payload?: object; enabled?: boolean }
) =>
  request<JobItem>(`/jobs/${encodeURIComponent(id)}`, {
    method: 'PUT',
    body: JSON.stringify(patch),
  })

export const deleteJob = (id: string) =>
  request<{ deleted: string }>(`/jobs/${encodeURIComponent(id)}`, { method: 'DELETE' })

export const runJob = (id: string) =>
  request<JobRunResult>(`/jobs/${encodeURIComponent(id)}/run`, { method: 'POST' })

export interface JobTickResult {
  executed: number
  jobs: { job_id: string; name: string; status: string }[]
}

export const tickJobs = () => request<JobTickResult>('/jobs/tick', { method: 'POST' })
