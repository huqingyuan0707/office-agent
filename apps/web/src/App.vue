<script setup lang="ts">
// 职责：全局壳 —— 顶栏（品牌 + 导航 tab + 用户名 + 退出）、全局错误红条、路由出口
// 链路：router 渲染本壳 → 各视图经 inject('shellError') 上报页面级错误（列表加载失败等）；
//      登录页（route.meta.public）不显示顶栏，只渲染登录视图
// 对齐：AGENTS.md §4 前端红线（401 已由 main.ts 中央处理，本层不重复处理；样式 var(--*) token）
import { provide, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { clearToken, clearUser, getUser } from './api'

const route = useRoute()
const router = useRouter()

const currentUser = ref(getUser())
const errBar = ref('')

// 页面级错误统一出口：视图经 inject 上报，本壳在顶栏下展示；切页即清，上一页错误不泄漏到下一页
const showError = (msg: string) => {
  errBar.value = msg
}
const clearError = () => {
  errBar.value = ''
}
provide('shellError', { errBar, showError, clearError })

// 切页清错 + 重新读用户名（登录页写入 localStorage 后回主区时刷新顶栏显示）
watch(
  () => route.path,
  () => {
    clearError()
    currentUser.value = getUser()
  },
)

const isPublic = () => route.meta.public === true

const doLogout = () => {
  clearToken()
  clearUser()
  currentUser.value = ''
  router.push('/login')
}
</script>

<template>
  <div v-if="!isPublic()" class="panel">
    <!-- 顶栏（吸顶：长列表滚动时仍可见导航、当前用户与退出） -->
    <header class="topbar">
      <span class="topbar-title">
        <span class="brand-mark small">OA</span>
        智能办公 Agent
      </span>
      <nav class="nav-tabs">
        <RouterLink class="nav-tab" to="/tools">工具</RouterLink>
        <RouterLink class="nav-tab" to="/tasks">任务</RouterLink>
        <RouterLink class="nav-tab" to="/approvals">审批</RouterLink>
        <RouterLink class="nav-tab" to="/agents">智能体</RouterLink>
        <RouterLink class="nav-tab" to="/reports">报表</RouterLink>
        <RouterLink class="nav-tab" to="/admin">管理</RouterLink>
        <RouterLink class="nav-tab" to="/governance">治理</RouterLink>
      </nav>
      <span class="topbar-right">
        <span class="user">{{ currentUser || '已登录' }}</span>
        <button class="btn ghost" @click="doLogout">退出</button>
      </span>
    </header>

    <div v-if="errBar" class="err-bar">
      <span class="err-text">{{ errBar }}</span>
      <button class="err-close" title="关闭" @click="errBar = ''">×</button>
    </div>

    <router-view />
  </div>

  <!-- 登录页：独占整屏，无顶栏 -->
  <router-view v-else />
</template>
