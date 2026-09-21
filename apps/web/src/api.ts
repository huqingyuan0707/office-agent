// 职责：后端 API 统一请求封装（token / base 配置读写、Authorization 注入、统一信封解析、401 处理）
// 链路：App.vue → request() → fetch(base + /api/v1 + path) → 解析 {code,msg,data,trace_id} 信封
// 契约：登录 POST /auth/login；工具 GET /agent/tools、POST /agent/tools/{name}/invoke；
//      任务 GET /tasks；审批 GET /approvals、POST /approvals/{id}/approve|reject

const TOKEN_KEY = 'office_token'
const BASE_KEY = 'office_base'
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
const CODE_MESSAGES: Record<number, string> = {
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
}

export const getToken = (): string => localStorage.getItem(TOKEN_KEY) ?? ''
export const setToken = (token: string): void => localStorage.setItem(TOKEN_KEY, token)
export const clearToken = (): void => localStorage.removeItem(TOKEN_KEY)
export const getBase = (): string => localStorage.getItem(BASE_KEY) ?? DEFAULT_BASE
export const setBase = (base: string): void => localStorage.setItem(BASE_KEY, base)

// 401 回调：由 App.vue 注册，用于清 token 后切回登录卡
let unauthorizedHandler: (() => void) | null = null
export const setUnauthorizedHandler = (fn: () => void): void => {
  unauthorizedHandler = fn
}

// 统一请求：自动带 Authorization 头；code!==0 抛错（服务端 msg 优先，缺省走中文错误表）；
// HTTP 401 或 code 1002 视为会话失效 → 清 token 并回调（登录接口本身无 token，不受影响）
export async function request<T = unknown>(path: string, options: RequestInit = {}): Promise<T> {
  const base = getBase().replace(/\/+$/, '')
  const headers: Record<string, string> = {}
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
    throw new Error(envelope.msg || CODE_MESSAGES[code] || `请求失败（code ${code}）`)
  }
  return envelope.data as T
}