<script setup lang="ts">
// 职责：工具页（Element Plus 版）—— 工具列表（卡片可选、选中高亮、scope/需审批/远程标签）+
//       调用面板（JSON 参数、调用中禁用、结果区状态/耗时/尝试次数/trace/溯源、JSON 限高滚动）
// 链路：router /tools → api.listTools / api.invokeTool；列表失败 → 壳红条 + 列表置空，
//       调用失败 → 本页结果区红条（两路错误互不影响）
// 对齐：AGENTS.md §4 前端红线（真实接口零 mock、禁直写 fetch、var(--*) token + scoped）
import { inject, onMounted, ref } from 'vue'
import { Check, Tools } from '@element-plus/icons-vue'
import { invokeTool, listTools } from '../api'
import type { InvokeResult, ToolItem } from '../api'
import ProvenanceBlock from '../components/ProvenanceBlock.vue'

const shell = inject('shellError') as {
  showError: (msg: string) => unknown
  clearError: () => unknown
}

const tools = ref<ToolItem[]>([])
const loading = ref(true)

// ---------- 工具调用 ----------
const invokeName = ref('')
const invokeArgs = ref('{}')
const invokeData = ref<InvokeResult | null>(null) // 成功结果（含 trace 与数据出处）
const invokeError = ref('') // 失败信息（结果区红条）
const invoking = ref(false)
const stringify = (value: unknown) => JSON.stringify(value, null, 2)

const loadTools = async () => {
  loading.value = true
  shell.clearError()
  try {
    tools.value = (await listTools()).items
  } catch (e) {
    tools.value = []
    shell.showError((e as Error).message || '工具列表加载失败')
  } finally {
    loading.value = false
  }
}

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
    invokeData.value = await invokeTool(invokeName.value, args)
  } catch (e) {
    invokeError.value = (e as Error).message || '调用失败'
  } finally {
    invoking.value = false
  }
}

onMounted(loadTools)
</script>

<template>
  <div class="grid">
    <!-- 左区：工具列表 -->
    <el-card shadow="never">
      <template #header>
        <div class="page-head">
          <span class="card-title">工具列表</span>
          <el-tag v-if="tools.length" size="small" type="info" round>{{ tools.length }} 个</el-tag>
        </div>
      </template>

      <el-skeleton v-if="loading" :rows="5" animated />
      <el-empty
        v-else-if="!tools.length"
        :image-size="80"
        description="暂无已注册工具（若预期有工具，请确认插件所需的上游提供方已配置）"
      />
      <div v-else class="tool-list">
        <el-card
          v-for="t in tools"
          :key="t.name"
          :class="['tool-item', { active: t.name === invokeName }]"
          shadow="hover"
          @click="pickTool(t.name)"
        >
          <div class="tool-top">
            <el-icon class="tool-icon"><Tools /></el-icon>
            <strong class="mono tool-name">{{ t.name }}</strong>
            <el-icon v-if="t.name === invokeName" class="tool-check"><Check /></el-icon>
          </div>
          <div class="tool-tags">
            <el-tag size="small" effect="plain">{{ t.scope }}</el-tag>
            <el-tag v-if="t.requires_approval" size="small" type="warning" effect="light">
              需审批
            </el-tag>
            <el-tag v-if="t.provider_id" size="small" type="primary" effect="plain">
              远程 · {{ t.provider_id }}
            </el-tag>
          </div>
          <p class="muted tool-desc">{{ t.description }}</p>
        </el-card>
      </div>
    </el-card>

    <!-- 右区：工具调用 -->
    <el-card shadow="never">
      <template #header>
        <div class="page-head">
          <span class="card-title">工具调用</span>
          <span class="muted head-hint">写工具调用只落审批单，批准后才执行</span>
        </div>
      </template>

      <el-form label-position="top" @submit.prevent>
        <el-form-item label="工具名称">
          <el-input v-model="invokeName" placeholder="点击左侧工具卡片，或直接输入工具名" />
        </el-form-item>
        <el-form-item label="参数 JSON">
          <el-input
            v-model="invokeArgs"
            type="textarea"
            :rows="5"
            spellcheck="false"
            placeholder='{"key": "value"}'
            class="args-input"
          />
        </el-form-item>
      </el-form>

      <el-button type="primary" :loading="invoking" :disabled="!invokeName" @click="doInvoke">
        {{ invoking ? '调用中…' : '调用' }}
      </el-button>

      <el-alert
        v-if="invokeData"
        class="result-box"
        type="success"
        show-icon
        :closable="false"
        :title="`调用成功 · ${invokeData.status}`"
      >
        <div class="result-meta">
          <el-tag size="small" effect="plain">耗时 {{ invokeData.latency_ms }}ms</el-tag>
          <el-tag size="small" effect="plain">尝试 {{ invokeData.attempts }} 次</el-tag>
          <el-tag v-if="invokeData.approval_required" size="small" type="warning" effect="light">
            已提交审批
          </el-tag>
        </div>
      </el-alert>

      <!-- 数据出处：跨系统取数的可见性（联动的验收面） -->
      <ProvenanceBlock
        v-if="invokeData?.provenance"
        :provenance="invokeData.provenance"
        :trace="invokeData.trace_id"
      />
      <pre v-if="invokeData" class="code-block">{{ stringify(invokeData.result ?? invokeData) }}</pre>

      <el-alert
        v-if="invokeError"
        class="result-box"
        type="error"
        :title="invokeError"
        show-icon
        :closable="false"
      />
    </el-card>
  </div>
</template>

<style scoped>
.tool-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 62vh;
  overflow-y: auto;
}
.tool-item {
  cursor: pointer;
  border-radius: var(--radius-sm);
  transition: border-color 0.15s ease, background 0.15s ease;
}
.tool-item :deep(.el-card__body) {
  padding: 12px 14px;
}
.tool-item.active {
  border-color: var(--brand);
  background: var(--brand-soft);
}
.tool-top {
  display: flex;
  align-items: center;
  gap: 8px;
}
.tool-icon {
  color: var(--brand);
}
.tool-name {
  font-size: 13.5px;
  font-weight: 600;
}
.tool-check {
  color: var(--brand);
  margin-left: auto;
}
.tool-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.tool-desc {
  font-size: 13px;
  margin: 8px 0 0;
}
.args-input :deep(textarea) {
  font-family: Consolas, 'Courier New', monospace;
  font-size: 13px;
}
.result-box {
  margin-top: 14px;
}
.result-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}
</style>