<script setup lang="ts">
// 职责：行政工单页（PRD §2.11 通用工具）—— IT 报修/资产申领/工单创建与清单，
//       走后端真实工具（desk.ticket 写恒送审 + desk.tickets 读），零 mock
// 链路：router /desk → api.invokeTool；落单后给审批 id 提示，到审批页批准后落台账
// 对齐：AGENTS.md §4 前端红线（真实接口零 mock、箭头函数、var(--*) token + scoped）+ PRD §2.11
import { onMounted, ref } from 'vue'
import { invokeTool } from '../api'

interface Ticket {
  id: string
  kind: string
  kind_label: string
  title: string
  status: string
  created_by: string
  created_at: string
}

const kindFilter = ref<string>('')
const tickets = ref<Ticket[]>([])
const loading = ref(false)
const kind = ref<string>('it_repair')
const title = ref('')
const detail = ref('')
const hint = ref('')
const pageError = ref('')

const loadTickets = async () => {
  loading.value = true
  pageError.value = ''
  try {
    const data = await invokeTool('office.desk.tickets', {
      ...(kindFilter.value ? { kind: kindFilter.value } : {}),
    })
    tickets.value = ((data.result ?? {}) as { tickets?: Ticket[] }).tickets ?? []
  } catch (e) {
    tickets.value = []
    pageError.value = (e as Error).message || '工单清单加载失败'
  } finally {
    loading.value = false
  }
}

const doTicket = async () => {
  if (!title.value.trim()) {
    pageError.value = '请填写工单标题'
    return
  }
  pageError.value = ''
  hint.value = ''
  try {
    const data = await invokeTool('office.desk.ticket', {
      kind: kind.value,
      title: title.value.trim(),
      detail: detail.value.trim(),
      idem_key: `web-${Date.now()}`,
    })
    const approvalId = (data as unknown as { approval_id?: string }).approval_id
    hint.value = approvalId ? `已提交审批（${approvalId}），批准后落台账` : '已提交，请到审批页处理'
    title.value = ''
    detail.value = ''
    await loadTickets()
  } catch (e) {
    pageError.value = (e as Error).message || '工单创建失败'
  }
}

onMounted(loadTickets)
</script>

<template>
  <div class="grid">
    <el-card shadow="never">
      <template #header>
        <div class="page-head">
          <span class="card-title">工单清单</span>
          <el-tag v-if="tickets.length" size="small" type="info" round>{{ tickets.length }} 条</el-tag>
        </div>
      </template>
      <div class="toolbar">
        <el-select v-model="kindFilter" placeholder="全部类型" clearable style="width: 160px" @change="loadTickets">
          <el-option label="IT 报修" value="it_repair" />
          <el-option label="资产申领" value="asset_claim" />
          <el-option label="工单创建" value="work_order" />
        </el-select>
        <el-button :loading="loading" @click="loadTickets">刷新</el-button>
      </div>
      <el-table v-if="tickets.length" :data="tickets" stripe size="small" style="width: 100%">
        <el-table-column prop="title" label="标题" min-width="180" show-overflow-tooltip />
        <el-table-column prop="kind_label" label="类型" width="100" />
        <el-table-column prop="status" label="状态" width="80" />
        <el-table-column prop="created_by" label="创建人" width="100" />
      </el-table>
      <el-empty v-else :image-size="80" description="暂无工单（空结果如实展示）" />
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="page-head">
          <span class="card-title">新建工单</span>
        </div>
      </template>
      <el-select v-model="kind" class="block-gap" style="width: 100%">
        <el-option label="IT 报修" value="it_repair" />
        <el-option label="资产申领" value="asset_claim" />
        <el-option label="工单创建" value="work_order" />
      </el-select>
      <el-input v-model="title" placeholder="标题（1-200 字）" class="block-gap" />
      <el-input v-model="detail" type="textarea" :rows="3" placeholder="详情说明" class="block-gap" />
      <el-button type="primary" @click="doTicket">提交审批</el-button>
      <p v-if="hint" class="muted">{{ hint }}</p>
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
