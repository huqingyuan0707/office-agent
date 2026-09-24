<script setup lang="ts">
// 职责：应用主壳（Element Plus 侧边菜单布局）——左侧分组菜单 + 顶栏（标题/收起/用户/退出）+ 内容区
// 链路：router '/' → 本壳 → router-view 渲染各域视图；菜单项与路由同源（router/menu.ts MENU_GROUPS）；
//      视图经 inject('shellError') 上报页面级错误，本壳在内容区顶部展示红条，切页即清
// 对齐：AGENTS.md §4 前端红线（401 中央处理在 main.ts，本壳不重复跳转；样式 var(--*) token）
import { computed, provide, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Expand, Fold, SwitchButton } from '@element-plus/icons-vue'
import { clearToken, clearUser, getUser } from '../api'
import { findMenuTitle, MENU_GROUPS } from '../router/menu'

const route = useRoute()
const router = useRouter()

const currentUser = ref(getUser())
const errBar = ref('')
const collapsed = ref(false)

// 页面级错误统一出口：视图经 inject 上报，本壳在内容区展示；切页即清，不泄漏到下一页
const showError = (msg: string) => {
  errBar.value = msg
}
const clearError = () => {
  errBar.value = ''
}
provide('shellError', { errBar, showError, clearError })

const activeMenu = computed(() => route.path)
const header = computed(() => findMenuTitle(route.path))

watch(
  () => route.path,
  () => {
    clearError()
    currentUser.value = getUser()
  },
)

const doLogout = () => {
  clearToken()
  clearUser()
  currentUser.value = ''
  router.push('/login')
}
</script>

<template>
  <el-container class="layout">
    <el-aside :width="collapsed ? '64px' : '220px'" class="layout-aside">
      <div class="aside-brand">
        <span class="brand-mark small">OA</span>
        <span v-show="!collapsed" class="aside-brand-text">智能办公 Agent</span>
      </div>
      <el-menu
        :default-active="activeMenu"
        :collapse="collapsed"
        :collapse-transition="false"
        router
        class="aside-menu"
      >
        <el-sub-menu v-for="group in MENU_GROUPS" :key="group.key" :index="group.key">
          <template #title>
            <el-icon><component :is="group.icon" /></el-icon>
            <span>{{ group.label }}</span>
          </template>
          <el-menu-item v-for="entry in group.entries" :key="entry.path" :index="entry.path">
            <el-icon><component :is="entry.meta.icon" /></el-icon>
            <template #title>{{ entry.meta.title }}</template>
          </el-menu-item>
        </el-sub-menu>
      </el-menu>
    </el-aside>

    <el-container class="layout-body">
      <el-header class="layout-header" height="56px">
        <div class="header-title">
          <span class="header-group">{{ header.group }}</span>
          <span class="header-page">{{ header.title }}</span>
        </div>
        <div class="header-right">
          <el-button text :icon="collapsed ? Expand : Fold" @click="collapsed = !collapsed">
            {{ collapsed ? '' : '收起菜单' }}
          </el-button>
          <span class="user">{{ currentUser || '已登录' }}</span>
          <el-button text :icon="SwitchButton" @click="doLogout">退出</el-button>
        </div>
      </el-header>

      <el-main class="layout-main">
        <div class="page-wrap">
          <div v-if="errBar" class="err-bar">
            <span class="err-text">{{ errBar }}</span>
            <button class="err-close" title="关闭" @click="errBar = ''">×</button>
          </div>
          <router-view />
        </div>
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.layout {
  height: 100vh;
}
.layout-aside {
  background: #fff;
  border-right: 1px solid var(--line);
  overflow-x: hidden;
  overflow-y: auto;
  transition: width 0.2s;
}
.aside-brand {
  display: flex;
  align-items: center;
  height: 56px;
  padding: 0 12px;
  border-bottom: 1px solid var(--line);
  white-space: nowrap;
}
.aside-brand-text {
  font-weight: 600;
}
.aside-menu {
  border-right: none;
  --el-menu-bg-color: transparent;
  --el-menu-text-color: var(--sub);
  --el-menu-active-color: var(--brand);
  --el-menu-hover-bg-color: #f2f7ff;
}
.aside-menu:not(.el-menu--collapse) {
  width: 220px;
}
.layout-body {
  overflow: hidden;
}
.layout-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: rgba(244, 245, 247, 0.94);
  border-bottom: 1px solid var(--line);
  backdrop-filter: blur(4px);
}
.header-title {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.header-group {
  color: var(--muted);
  font-size: 12px;
}
.header-page {
  font-size: 16px;
  font-weight: 600;
}
.header-right {
  display: flex;
  align-items: center;
  gap: 8px;
}
.layout-main {
  padding: 16px;
  overflow-y: auto;
  background: var(--bg);
}
.page-wrap {
  max-width: 1080px;
  margin: 0 auto;
}
</style>
