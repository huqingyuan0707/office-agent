<script setup lang="ts">
// 职责：工具页 —— 工具列表（卡片可选、选中高亮、scope/需审批/远程徽标）+ 调用面板（JSON 参数、
//       调用中禁用、结果区 status/耗时/尝试次数/trace/溯源、JSON 限高滚动、失败红条）
// 链路：router /tools → api.listTools / api.invokeTool；列表失败 → 壳红条 + 列表置空，
//       调用失败 → 本页结果区红条（两路错误互不影响）
// 对齐：AGENTS.md §4 前端红线（真实接口零 mock、禁直写 fetch、样式 var(--*) token）
import { inject, onMounted, ref } from 'vue'
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
    <section class="card">
      <h2>
        工具列表
        <span v-if="tools.length" class="badge">{{ tools.length }}</span>
      </h2>
      <p v-if="loading" class="empty">加载中…</p>
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

    <!-- 右区：工具调用 -->
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
        </div>
        <!-- 数据出处：跨系统取数的可见性（联动的验收面） -->
        <ProvenanceBlock
          v-if="invokeData.provenance"
          :provenance="invokeData.provenance"
          :trace="invokeData.trace_id"
        />
        <pre>{{ stringify(invokeData.result ?? invokeData) }}</pre>
      </div>
      <div v-if="invokeError" class="result fail">{{ invokeError }}</div>
    </section>
  </div>
</template>
