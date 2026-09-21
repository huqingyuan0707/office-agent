<script setup lang="ts">
// 职责：登录页 —— 服务地址/用户名/密码登录卡（错误红条可关闭、登录中禁用、回车提交）
// 链路：router /login → api.login()（base 先落盘再发请求）→ setToken/setUser → 跳 /tools；
//      会话失效（401 中央处理）带 expired 标记回跳本页时复显「登录已失效」提示
// 对齐：AGENTS.md §4 前端红线（真实接口零 mock、401 不在本页自跳）
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getBase, login, setBase, setToken, setUser } from '../api'

const route = useRoute()
const router = useRouter()

const base = ref(getBase())
const username = ref('')
const password = ref('')
const errBar = ref('')
const expiredNotice = ref(route.query.expired === '1') // 会话失效回跳提示（可关闭）
const loggingIn = ref(false)
const canLogin = computed(() => !!username.value && !!password.value)

const closeNotice = () => {
  errBar.value = ''
  expiredNotice.value = false
}

const doLogin = async () => {
  if (!canLogin.value || loggingIn.value) return
  errBar.value = ''
  expiredNotice.value = false
  // 服务地址必须先落盘再发请求：request() 读的是已保存的 base，
  // 若放到成功之后再存，本次登录仍会打向默认/上次地址，输入框形同虚设
  base.value = base.value.trim().replace(/\/+$/, '')
  setBase(base.value)
  loggingIn.value = true
  try {
    const data = await login(username.value, password.value)
    setToken(data.token)
    setUser(username.value)
    password.value = '' // 口令不留在内存里的表单上
    router.push('/tools')
  } catch (e) {
    errBar.value = (e as Error).message || '登录失败'
  } finally {
    loggingIn.value = false
  }
}
</script>

<template>
  <div class="login-wrap">
    <div class="card login-card">
      <div class="brand">
        <span class="brand-mark">OA</span>
        <div>
          <h1 class="login-title">智能办公 Agent</h1>
          <p class="muted">工具注册 · Scope 鉴权 · 审批闸门 · 全程审计</p>
        </div>
      </div>

      <div v-if="errBar || expiredNotice" class="err-bar">
        <span class="err-text">{{ errBar || '登录已失效，请重新登录' }}</span>
        <button class="err-close" title="关闭" @click="closeNotice">×</button>
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
</template>
