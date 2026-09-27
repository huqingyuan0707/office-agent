<script setup lang="ts">
// 职责：任务控制台（Agent 执行中枢）——统计卡 + 状态/来源/类型/时间过滤 + 搜索
//       + 任务全生命周期表格（名称/来源/关联对象/状态色/动态进度/结果入口/操作）
//       + 详情对话框（input/output/error/checkpoint 解析与失败建议）+ 结果导出
//       + 服务不可用友好降级 + 空态引导（去对话办理发起任务）+ 执行中自动轮询
// 链路：router /tasks → api.listTasks/taskStats/getTask/retryTask/cancelTask/deleteTask/exportTask
//       （scope=all 仅 admin；非 admin 后端回退本人维度）；列表失败置空 + 报错，绝不 mock
// 对齐：AGENTS.md §4 前端红线（真实接口/失败置空/401 中央处理/var(--*) 样式）
import { computed, inject, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox, ElNotification } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import {
  cancelTask,
  deleteTask,
  exportTask,
  getTask,
  getBase,
  listTasks,
  retryTask,
  taskStats,
} from '../api'
import type { TaskItem, TaskStats, TaskResultLink } from '../api'

const shell = inject('shellError') as {
  showError: (msg: string) => unknown
  clearError: () => unknown
}
const router = useRouter()

/* ---------- 状态 ---------- */
const loading = ref(true)
const connError = ref('') // 服务不可用 → 降级卡（重试/引导）
const items = ref<TaskItem[]>([])
const stats = ref<TaskStats | null>(null)
const busyId = ref('')
const pageError = ref('')

// 筛选
const fStatus = ref('')
const fSource = ref('')
const fType = ref('')
const fDays = ref(0) // 0=全部 1=今日 7=本周 30=本月
const fQ = ref('')
const fScope = ref<'mine' | 'all'>('mine')

const detailOpen = ref(false)
const detail = ref<TaskItem | null>(null)
const detailLoading = ref(false)

// 类型下拉来源：真实数据聚合（空则只有「全部」）
const typeOptions = computed(() => {
  const set = new Set<string>()
  items.value.forEach((t) => t.type && set.add(t.type))
  return [...set].sort()
})

// 状态展示元数据（颜色口径：执行中蓝 / 成功绿 / 失败红 / 待确认橙 / 排队与取消灰）
const STATUS_META: Record<string, { label: string; color: string }> = {
  pending: { label: '排队中', color: 'info' },
  running: { label: '执行中', color: 'primary' },
  waiting_approval: { label: '待确认', color: 'warning' },
  done: { label: '已完成', color: 'success' },
  failed: { label: '失败', color: 'danger' },
  cancelled: { label: '已取消', color: 'info' },
}
const statusMeta = (s: string) => STATUS_META[s] || { label: s, color: 'info' }

// el-table 插槽的 row 被 EP 推断为 DefaultRow，统一在调用边界收窄回领域类型
const asTask = (raw: unknown) => raw as TaskItem

const isLongStatus = (s: string) => s === 'running' || s === 'pending' || s === 'waiting_approval'
// 进度条口径：done=100；其余按后端 progress 等比例（无进度则 0，长任务运行中可动态）
const progressOf = (t: TaskItem) => (t.status === 'done' ? 100 : Math.round((t.progress || 0) * 100))

// 结果入口能否跳转（审批单/外链；文档类走详情对话框查看）
const openResult = (link: TaskResultLink, task: TaskItem) => {
  if (link.kind === 'approval' || (link.href && link.kind === 'link')) {
    if (link.href) void router.push(link.href)
    else void router.push(`/approvals?id=${task.ref_id || ''}`)
    return
  }
  openDetail(task.id)
}

/* ---------- 数据加载（并列拉 stats + 列表，任一分失败置空并如实提示） ---------- */
let lastSnap = ''
const loadAll = async () => {
  shell.clearError()
  try {
    const [list, stat] = await Promise.all([
      listTasks({
        status: fStatus.value,
        type: fType.value,
        source: fSource.value,
        q: fQ.value,
        days: fDays.value,
        scope: fScope.value,
        size: 200,
      }),
      taskStats(fScope.value),
    ])
    const snap = list
      .map((t) => `${t.id}:${t.status}`)
      .sort()
      .join('|')
    // 轮询期间状态变化 → 右上角通知（已在初始化后的 diff 才推，避免首屏误报）
    if (lastSnap && snap !== lastSnap) {
      const before = new Set(lastSnap.split('|').map((s) => s.split(':')[0]))
      list.forEach((t) => {
        if (!before.has(t.id) && ['done', 'failed', 'cancelled'].includes(t.status)) {
          ElNotification({
            title: statusMeta(t.status).label,
            message: `任务「${t.name}」${statusMeta(t.status).label}`,
            type: t.status === 'failed' ? 'error' : 'success',
          })
        }
      })
    }
    lastSnap = snap
    items.value = list
    stats.value = stat
    connError.value = ''
  } catch (e) {
    connError.value = (e as Error).message || '任务列表加载失败'
    items.value = []
    stats.value = null
  } finally {
    loading.value = false
  }
}

const applyFilter = () => {
  pageError.value = ''
  loadAll()
}

// 执行中自动轮询（关闭页面不阻塞：仅当前会话轮询，后台长任务由后端调度）
let timer: number | undefined
const startPolling = () => {
  stopPolling()
  timer = window.setInterval(() => {
    if (items.value.some((t) => isLongStatus(t.status))) {
      loadAll().catch(() => undefined)
    }
  }, 5000)
}
const stopPolling = () => {
  if (timer) window.clearInterval(timer)
  timer = undefined
}

/* ---------- 操作（真实接口 + 确认） ---------- */
const withBusy = async (fn: () => Promise<unknown>) => {
  pageError.value = ''
  busyId.value = 'op'
  try {
    await fn()
    await loadAll()
  } catch (e) {
    pageError.value = (e as Error).message || '操作失败'
  } finally {
    busyId.value = ''
  }
}

const onRetry = (t: TaskItem) => {
  ElMessageBox.confirm(`将把任务「${t.name}」重新排队执行，确定？`, '重试任务', {
    confirmButtonText: '重试',
    cancelButtonText: '取消',
    type: 'warning',
  })
    .then(() => withBusy(() => retryTask(t.id)))
    .catch(() => undefined)
}

const onCancel = (t: TaskItem) => {
  ElMessageBox.confirm(`将取消任务「${t.name}」（已产生的部分结果保留），确定？`, '取消任务', {
    confirmButtonText: '取消任务',
    cancelButtonText: '返回',
    type: 'warning',
  })
    .then(async () => {
      await withBusy(() => cancelTask(t.id))
      ElMessage.success('任务已取消')
    })
    .catch(() => undefined)
}

const onDelete = (t: TaskItem) => {
  ElMessageBox.confirm(`将删除任务「${t.name}」的记录，删除后不可恢复，确定？`, '删除任务', {
    confirmButtonText: '删除',
    cancelButtonText: '返回',
    type: 'warning',
    confirmButtonClass: 'el-button--danger',
  })
    .then(async () => {
      await withBusy(() => deleteTask(t.id))
      ElMessage.success('任务记录已删除')
    })
    .catch(() => undefined)
}

const openDetail = async (id: string) => {
  detailOpen.value = true
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await getTask(id)
  } catch (e) {
    ElMessage.error((e as Error).message || '任务详情加载失败')
  } finally {
    detailLoading.value = false
  }
}

// 结果导出：base64 文本 → Blob 下载（内容由后端真实渲染，不触盘）
const fmtDropdown = (t: TaskItem, fmt: string) => {
  void (async () => {
    timingExport(t, fmt)
  })()
}
const timingExport = async (t: TaskItem, fmt: string) => {
  try {
    const exp = await exportTask(t.id, fmt)
    const bytes = Uint8Array.from(atob(exp.content), (c) => c.charCodeAt(0))
    const blob = new Blob([bytes], { type: exp.mime })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${exp.filename}.${exp.format}`
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) {
    ElMessage.error((e as Error).message || '导出失败')
  }
}

// 快捷入口：真实引导去对话办理（CHatView 不接 URL 参数，直达再自然交流）
const renderDest = (v: string) => void router.push(`/chat?goal=${encodeURIComponent(v)}`)

/* ---------- 生命周期 ---------- */
onMounted(async () => {
  loading.value = true
  await loadAll()
  startPolling()
})
onBeforeUnmount(stopPolling)
</script>

<template>
  <div class="tasks-view page-wrap">
    <!-- 服务不可用降级卡（友好提示 + 重试/去设置/联系管理员，绝不白屏） -->
    <div v-if="connError" class="conn-card">
      <h3 class="conn-title">无法连接到任务服务</h3>
      <p class="muted">
        {{ connError }}。当前配置的服务地址：<span class="mono">{{ getBase() }}</span>
      </p>
      <div class="conn-actions">
        <el-button type="primary" :loading="loading" @click="applyFilter">重试连接</el-button>
        <el-button @click="void router.push('/login')">前往登录页重新配置服务地址</el-button>
      </div>
      <p class="muted conn-tip">若仍无法连接，请联系管理员检查服务是否已启动。</p>
    </div>

    <template v-else>
      <!-- 统计卡（真实 stats：总览/今日/本周/本月/执行中/失败/各状态） -->
      <div class="stat-grid">
        <div class="stat-card">
          <span class="stat-num">{{ stats?.total ?? 0 }}</span>
          <span class="stat-label">任务总数</span>
        </div>
        <div class="stat-card">
          <span class="stat-num brand">{{ stats?.today ?? 0 }}</span>
          <span class="stat-label">今日新增</span>
        </div>
        <div class="stat-card">
          <span class="stat-num">{{ stats?.week ?? 0 }}</span>
          <span class="stat-label">近 7 天</span>
        </div>
        <div class="stat-card">
          <span class="stat-num">{{ stats?.running ?? 0 }}</span>
          <span class="stat-label">执行中/待确认</span>
        </div>
        <div class="stat-card">
          <span class="stat-num danger">{{ stats?.failed ?? 0 }}</span>
          <span class="stat-label">失败需要处理</span>
        </div>
      </div>

      <!-- 筛选栏 -->
      <div class="toolbar">
        <el-select v-model="fStatus" placeholder="状态" clearable style="width: 120px" @change="applyFilter">
          <el-option label="排队中" value="pending" />
          <el-option label="执行中" value="running" />
          <el-option label="待确认" value="waiting_approval" />
          <el-option label="已完成" value="done" />
          <el-option label="失败" value="failed" />
          <el-option label="已取消" value="cancelled" />
        </el-select>
        <el-select v-model="fSource" placeholder="来源" clearable style="width: 120px" @change="applyFilter">
          <el-option label="对话办理" value="chat" />
          <el-option label="文档中心" value="docs" />
          <el-option label="任务拆解" value="breakdown" />
          <el-option label="定时任务" value="scheduled" />
          <el-option label="智能体" value="agent" />
          <el-option label="手动登记" value="manual" />
        </el-select>
        <el-select v-model="fType" placeholder="类型" clearable style="width: 150px" @change="applyFilter">
          <el-option v-for="t in typeOptions" :key="t" :label="t" :value="t" />
        </el-select>
        <el-radio-group v-model="fDays" class="days-radio" @change="applyFilter">
          <el-radio-button :value="0">全部时间</el-radio-button>
          <el-radio-button :value="1">今日</el-radio-button>
          <el-radio-button :value="7">本周</el-radio-button>
          <el-radio-button :value="30">本月</el-radio-button>
        </el-radio-group>
        <el-input
          v-model="fQ"
          placeholder="搜索任务名称 / 类型 / 关联对象"
          clearable
          style="width: 220px"
          @keyup.enter="applyFilter"
          @clear="applyFilter"
        >
          <template #append>
            <el-button @click="applyFilter">搜索</el-button>
          </template>
        </el-input>
        <el-switch
          v-model="fScope"
          inactive-value="mine"
          active-value="all"
          active-text="全租户"
          inactive-text="我的"
          @change="loadAll"
        />
      </div>
      <p v-if="fScope === 'all'" class="scope-hint muted">全租户视图：仅管理员可见全部任务；非管理员自动回到本人维度。</p>

      <p v-if="pageError" class="page-error">{{ pageError }}</p>

      <!-- 表格 / 空态 -->
      <div v-if="items.length" class="table-card">
        <el-table :data="items" v-loading="loading" style="width: 100%">
          <el-table-column label="任务" min-width="220">
            <template #default="{ row }">
              <div class="task-name">{{ row.name }}</div>
              <div class="task-meta">
                <el-tag size="small" :type="row.source === 'chat' ? 'primary' : 'info'" effect="plain">
                  {{ row.source_label }}
                </el-tag>
                <el-tag size="small" type="info" effect="plain" v-if="row.type">
                  {{ row.type }}
                </el-tag>
                <span class="mono task-id">{{ row.id }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="关联对象" min-width="120">
            <template #default="{ row }">
              <template v-if="row.ref_label">
                <el-tag size="small" effect="plain">{{ row.ref_label }}</el-tag>
              </template>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="130">
            <template #default="{ row }">
              <div class="status-cell">
                <el-tag :type="statusMeta(row.status).color" effect="light" size="small">
                  {{ row.status_label }}
                </el-tag>
                <el-progress
                  :percentage="progressOf(asTask(row))"
                  :stroke-width="4"
                  :show-text="false"
                  class="task-progress"
                  v-if="isLongStatus(row.status)"
                />
              </div>
            </template>
          </el-table-column>
          <el-table-column label="进度" width="90">
            <template #default="{ row }">
              <span :class="row.status === 'failed' ? 'danger' : ''">
                {{ progressOf(asTask(row)) }}%
              </span>
            </template>
          </el-table-column>
          <el-table-column label="创建时间" width="150">
            <template #default="{ row }">
              <span class="muted">{{ row.created_at }}</span>
            </template>
          </el-table-column>
          <el-table-column label="结果入口" width="130">
            <template #default="{ row }">
              <div v-if="row.result_links.length" class="result-links">
                <el-button
                  v-for="(link, i) in row.result_links"
                  :key="i"
                  link
                  type="primary"
                  size="small"
                  @click="openResult(link, asTask(row))"
                >
                  {{ link.label }}
                </el-button>
              </div>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="210" fixed="right">
            <template #default="{ row }">
              <el-button
                v-if="['failed', 'cancelled', 'done'].includes(row.status)"
                link
                type="primary"
                size="small"
                :disabled="busyId !== ''"
                @click="onRetry(asTask(row))"
              >
                重试
              </el-button>
              <el-button
                v-if="isLongStatus(row.status)"
                link
                type="warning"
                size="small"
                :disabled="busyId !== ''"
                @click="onCancel(asTask(row))"
              >
                取消
              </el-button>
              <el-dropdown trigger="click" @command="(c: string) => fmtDropdown(asTask(row), c)">
                <el-button link type="primary" size="small">导出</el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="md">Markdown</el-dropdown-item>
                    <el-dropdown-item command="csv">CSV</el-dropdown-item>
                    <el-dropdown-item command="xlsx">Excel（xlsx）</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
              <el-button link type="primary" size="small" @click="openDetail(row.id)">详情</el-button>
              <el-button
                link
                type="danger"
                size="small"
                :disabled="busyId !== ''"
                @click="onDelete(asTask(row))"
              >
                删除
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <!-- 空态：无任务 → 引导去对话办理发起 -->
      <el-empty v-else :image-size="110" description="您还没有任务">
        <template #default>
          <div class="empty-guide">
            <p class="muted">去对话办理发起一个吧，Agent 执行的任务会自动进入这里：</p>
            <div class="empty-actions">
              <el-button type="primary" @click="void router.push('/chat')">
                <el-icon><Plus /></el-icon>&nbsp;去对话办理
              </el-button>
            </div>
            <p class="muted">常用任务模板（一键直达对话办理）：</p>
            <div class="empty-actions">
              <el-button @click="renderDest('帮我生成本周工作周报')">生成周报</el-button>
              <el-button @click="renderDest('帮我把项目文档做批量处理并按模板汇总')">
                批量处理文档
              </el-button>
              <el-button @click="renderDest('帮我把「官网改版」项目拆解成可执行任务清单')">
                拆解项目任务
              </el-button>
            </div>
          </div>
        </template>
      </el-empty>
    </template>

    <!-- 详情对话框（真实详情接口：input/output/error/checkpoint 解析） -->
    <el-dialog
      v-model="detailOpen"
      :title="detail?.name || '任务详情'"
      width="720px"
      top="6vh"
    >
      <div v-loading="detailLoading">
        <template v-if="detail">
          <div class="detail-meta">
            <el-tag :type="statusMeta(detail.status).color" effect="light" size="small">
              {{ detail.status_label }}
            </el-tag>
            <el-tag v-if="detail.source_label" size="small" effect="plain">
              {{ detail.source_label }}
            </el-tag>
            <el-tag v-if="detail.ref_label" size="small" effect="plain">
              {{ detail.ref_label }}
            </el-tag>
            <span class="mono muted detail-id">{{ detail.id }}</span>
            <span class="muted">{{ detail.created_at }}</span>
          </div>

          <template v-if="detail.error && Object.keys(detail.error).length">
            <h4 class="sub-title">失败原因</h4>
            <div class="err-block">
              <p class="err-msg">{{ detail.error_hint || '任务执行失败，请查看下方错误详情' }}</p>
              <pre class="code-block">{{ JSON.stringify(detail.error, null, 2) }}</pre>
            </div>
          </template>

          <template v-if="detail.output && Object.keys(detail.output).length">
            <h4 class="sub-title">执行结果</h4>
            <pre class="code-block">{{ JSON.stringify(detail.output, null, 2) }}</pre>
          </template>

          <template
            v-if="detail.checkpoint && Object.keys(detail.checkpoint).length"
          >
            <h4 class="sub-title">执行轨迹（断点）</h4>
            <pre class="code-block">{{ JSON.stringify(detail.checkpoint, null, 2) }}</pre>
          </template>

          <template v-if="detail.input && Object.keys(detail.input).length">
            <h4 class="sub-title">任务输入</h4>
            <pre class="code-block">{{ JSON.stringify(detail.input, null, 2) }}</pre>
          </template>
        </template>
        <el-empty v-else description="暂无详情" />
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
/* 样式一律走全局 token（--brand/--ok/--danger/--muted/--line 等），页面内不硬编码主题色 */
.tasks-view {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.conn-card {
  background: #fff;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 28px 20px;
  text-align: center;
}
.conn-title {
  color: var(--fg);
}
.conn-actions {
  display: flex;
  gap: 10px;
  justify-content: center;
  margin: 14px 0 4px;
}
.conn-tip {
  font-size: 12px;
}

.stat-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 12px;
}
@media (max-width: 900px) {
  .stat-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}
.stat-card {
  background: #fff;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.stat-num {
  font-size: 26px;
  font-weight: 700;
  line-height: 1.2;
}
.stat-num.brand {
  color: var(--brand);
}
.stat-num.danger {
  color: var(--danger);
}
.stat-label {
  font-size: 12px;
  color: var(--muted);
}

.toolbar {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
}
.days-radio {
  display: inline-flex;
}
.scope-hint {
  font-size: 12px;
  margin: 2px 0 0;
}
.page-error {
  background: var(--danger-bg);
  color: var(--danger);
  border-radius: var(--radius-sm);
  padding: 8px 12px;
  font-size: 13px;
  margin: 0;
}

.table-card {
  background: #fff;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 6px 12px 12px;
}
.task-name {
  font-weight: 600;
  font-size: 13.5px;
}
.task-meta {
  display: flex;
  gap: 6px;
  align-items: center;
  margin-top: 4px;
  flex-wrap: wrap;
}
.task-id {
  font-size: 11px;
  color: var(--muted);
}
.status-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: flex-start;
}
.task-progress {
  width: 96px;
}
.result-links {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
}
.danger {
  color: var(--danger);
}

.empty-guide {
  padding: 4px 0 10px;
}
.empty-actions {
  display: flex;
  gap: 10px;
  justify-content: center;
  flex-wrap: wrap;
  margin: 8px 0 18px;
}

.detail-meta {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.detail-id {
  font-size: 11px;
}
.err-block {
  background: var(--danger-bg);
  border: 1px solid #f0c4c0;
  border-radius: var(--radius-sm);
  padding: 8px 10px;
  margin-bottom: 12px;
}
.err-msg {
  margin: 0 0 6px;
  color: var(--danger);
  font-weight: 600;
}
.cancel-dialog .el-button--danger {
  margin-left: 0;
}
</style>