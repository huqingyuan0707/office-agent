<script setup lang="ts">
// 职责：驳回理由弹窗（el-dialog 版）—— 展示申请内容（驳回也是决策，理由要对着内容写）、
//       理由必填校验、打开自动聚焦、失败不丢输入（接口错误由父级经 props 传入就地显示）
// 链路：ApprovalsView 打开（props.target）→ 本组件校验后 emit confirm(reason) / cancel；
//      confirm 提交失败时父级不关窗、error prop 就地提示，已输入理由保留
// 对齐：AGENTS.md §4 前端红线（组件零请求、零 mock；样式 var(--*) token + scoped）
import { computed, ref, watch } from 'vue'
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
// 结构性类型收窄即可（只用到 focus），避免为焦点引入 EP 组件类型
const reasonInput = ref<{ focus: () => unknown } | null>(null)

// 打开（target 变为非空）时清空上次输入；弹窗动画结束（el-dialog opened）后自动聚焦
watch(
  () => props.target,
  (t) => {
    localError.value = ''
    if (t) reason.value = ''
  },
)

const onOpened = () => {
  reasonInput.value?.focus()
}

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
  <el-dialog
    :model-value="!!target"
    title="驳回审批单"
    width="480px"
    append-to-body
    :close-on-click-modal="false"
    :close-on-press-escape="!busy"
    @update:model-value="close"
    @opened="onOpened"
  >
    <template v-if="target">
      <el-descriptions :column="1" size="small" border class="target-desc">
        <el-descriptions-item label="审批类型">{{ target.action }}</el-descriptions-item>
        <el-descriptions-item label="申请对象">{{ target.target }}</el-descriptions-item>
        <el-descriptions-item label="单号">
          <span class="mono">{{ target.id }}</span>
        </el-descriptions-item>
      </el-descriptions>

      <div v-if="argsText" class="args-block">
        <span class="sub-title">申请内容</span>
        <pre class="code-block args-pre">{{ argsText }}</pre>
      </div>

      <el-alert
        v-if="localError || error"
        class="block-gap"
        type="error"
        :title="localError || error"
        show-icon
        :closable="false"
      />

      <el-form label-position="top" @submit.prevent>
        <el-form-item label="驳回理由（必填）">
          <el-input
            ref="reasonInput"
            v-model="reason"
            type="textarea"
            :rows="4"
            maxlength="200"
            show-word-limit
            placeholder="请说明驳回原因，将随审批结果留痕"
            @keydown.esc="close"
          />
        </el-form-item>
      </el-form>
    </template>

    <template #footer>
      <el-button :disabled="busy" @click="close">取消</el-button>
      <el-button type="danger" :loading="busy" @click="confirm">
        {{ busy ? '提交中…' : '确认驳回' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.target-desc {
  margin-bottom: 12px;
}
.args-block {
  margin-bottom: 12px;
}
.args-pre {
  max-height: 160px;
}
</style>