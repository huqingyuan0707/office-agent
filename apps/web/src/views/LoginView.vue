<script setup lang="ts">
// 职责：登录页（Element Plus 表单卡）—— 服务地址/用户名/密码，登录中禁用、回车提交、
//       失败或会话失效以 el-alert 呈现
// 链路：router /login → api.login()（base 先落盘再发请求）→ setToken/setUser → 跳 /chat；
//      会话失效（401 中央处理）带 expired 标记回跳本页时复显「登录已失效」提示
// 对齐：AGENTS.md §4 前端红线（真实接口零 mock、401 不在本页自跳、样式 var(--*) token + scoped）
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Lock, Monitor, User } from '@element-plus/icons-vue'
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

const alertText = computed(() => errBar.value || (expiredNotice.value ? '登录已失效，请重新登录' : ''))

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
    router.push('/chat')
  } catch (e) {
    errBar.value = (e as Error).message || '登录失败'
  } finally {
    loggingIn.value = false
  }
}
</script>

<template>
  <div class="login-wrap">
    <el-card class="login-card" shadow="never">
      <div class="brand">
        <span class="brand-mark">OA</span>
        <div class="brand-text">
          <h1 class="login-title">智能办公 Agent</h1>
          <p class="brand-sub">工具注册 · Scope 鉴权 · 审批闸门 · 全程审计</p>
        </div>
      </div>

      <el-alert
        v-if="alertText"
        class="block-gap"
        type="error"
        :title="alertText"
        show-icon
        closable
        @close="closeNotice"
      />

      <el-form label-position="top" size="large" @submit.prevent="doLogin">
        <el-form-item label="服务地址">
          <el-input v-model="base" :prefix-icon="Monitor" placeholder="http://127.0.0.1:8200" />
        </el-form-item>
        <el-form-item label="用户名">
          <el-input
            v-model="username"
            :prefix-icon="User"
            placeholder="请输入用户名"
            autocomplete="username"
            @keyup.enter="doLogin"
          />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="password"
            type="password"
            show-password
            :prefix-icon="Lock"
            placeholder="请输入密码"
            autocomplete="current-password"
            @keyup.enter="doLogin"
          />
        </el-form-item>
        <el-button
          class="login-btn"
          type="primary"
          size="large"
          :loading="loggingIn"
          :disabled="!canLogin"
          @click="doLogin"
        >
          {{ loggingIn ? '登录中…' : '登录' }}
        </el-button>
      </el-form>
    </el-card>
  </div>
</template>

<style scoped>
.login-wrap {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  background: linear-gradient(160deg, #eef3fb 0%, var(--bg) 55%);
}
.login-card {
  width: 400px;
  max-width: 100%;
  border-radius: 14px;
  box-shadow: var(--shadow-pop);
}
.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 22px;
}
.brand-mark {
  flex: none;
  width: 42px;
  height: 42px;
  border-radius: 10px;
  background: linear-gradient(135deg, var(--brand), #4f8ff5);
  color: #fff;
  font-weight: 700;
  font-size: 14px;
  letter-spacing: 0.5px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 3px 10px rgba(31, 111, 235, 0.3);
}
.brand-text {
  min-width: 0;
}
.login-title {
  font-size: 19px;
  margin: 0;
}
.brand-sub {
  margin: 2px 0 0;
  font-size: 12px;
  color: var(--muted);
}
.login-btn {
  width: 100%;
  margin-top: 4px;
  letter-spacing: 2px;
}
</style>