<script setup lang="ts">
// 职责：驳回理由弹窗 —— 展示申请内容（驳回也是决策，理由要对着内容写）、理由必填校验、
//       Esc 关闭、打开自动聚焦、失败不丢输入（接口错误由父级经 props 传入就地显示）
// 链路：ApprovalsView 打开（props.target）→ 本组件校验后 emit confirm(reason) / cancel；
//      confirm 提交失败时父级不关窗、error prop 就地提示，已输入理由保留
// 对齐：AGENTS.md §4 前端红线（组件零请求、零 mock；样式 var(--*) token）
import { computed, nextTick, ref, watch } from 'vue'
import type { ApprovalItem } from '../api'

const props = defineProps<{
  target: ApprovalItem | null // 目标审批单（null 即关闭）
  error: string // 父级提交失败的错误信息（就地显示，不丢输入）
  busy: boolean // 提交进行中（按钮禁用）
}>()

const emit = defineEmits(['confirm', 'cancel'])

// 申请内容（缩进 JSON 只读展示）：无参数时整块不渲染，不留空壳
const argsText = computed(() => {
  const args = props.target?.args
  if (typeof args !== 'object' || args === null || !Object.keys(args).length) return ''
  return JSON.stringify(args, null, 2)
})

const reason = ref('')
const localError = ref('')
const reasonInput = ref<HTMLTextAreaElement | null>(null)

// 打开（target 变为非空）时清空上次输入并自动聚焦；关闭时清校验错误
watch(
  () => props.target,
  async (t) => {
    localError.value = ''
    if (t) {
      reason.value = ''
      await nextTick() // 弹窗渲染后再聚焦，键盘可直接输入理由
      reasonInput.value?.focus()
    }
  },
)

const close = () => {
  emit('cancel')
}

// 驳回理由必填（服务端 1001 拦截）：先在本地收齐，避免空理由白跑一次请求
const confirm = () => {
  const text = reason.value.trim()
  if (!text) {
    localError.value = '驳回理由必填，请填写后再驳回'
    return
  }
  localError.value = ''
  emit('confirm', text)
}
</script>

<template>
  <div v-if="target" class="modal-mask" @click.self="close">
    <div class="card modal">
      <h2>驳回审批单</h2>
      <p class="muted">
        {{ target.action }} · {{ target.target }}
        <span class="mono">（{{ target.id }}）</span>
      </p>
      <div v-if="argsText" class="args-block">
        <span class="args-label">申请内容</span>
        <pre class="mono">{{ argsText }}</pre>
      </div>
      <div v-if="localError || error" class="err-bar">
        <span class="err-text">{{ localError || error }}</span>
      </div>
      <label class="field">
        <span>驳回理由（必填）</span>
        <textarea
          ref="reasonInput"
          v-model="reason"
          rows="4"
          placeholder="请说明驳回原因，将随审批结果留痕"
          @keydown.esc="close"
        ></textarea>
      </label>
      <div class="modal-actions">
        <button class="btn" :disabled="busy" @click="close">取消</button>
        <button class="btn danger" :disabled="busy" @click="confirm">
          {{ busy ? '提交中…' : '确认驳回' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 申请内容块：只读、限高可滚动，避免长入参把弹窗撑破 */
.args-block {
  margin: 0 0 12px;
}
.args-label {
  display: block;
  margin-bottom: 4px;
  color: var(--sub);
  font-size: 12px;
}
.args-block pre {
  max-height: 160px;
  overflow: auto;
  margin: 0;
  padding: 8px 10px;
  background: var(--bg);
  border: 1px solid var(--line);
  border-radius: 6px;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
