<script setup lang="ts">
// 职责：应用主壳（Element Plus 侧边菜单布局）——左侧品牌+分组菜单 + 顶栏（面包屑/收起/用户下拉）
//       + 内容区（页面级错误 el-alert + router-view 动效容器）
// 链路：router '/' → 本壳 → router-view 渲染各域视图；菜单项与路由同源（router/menu.ts MENU_GROUPS）；
//      视图经 inject('shellError') 上报页面级错误，本壳以 el-alert 展示，切页即清
// 对齐：AGENTS.md §4 前端红线（401 中央处理在 main.ts，本壳不重复跳转；样式 var(--*) token）
import { computed, provide, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowDown, Expand, Fold, SwitchButton, User } from '@element-plus/icons-vue'
import { clearToken, clearUser, getUser } from '../api'
import { findMenuTitle, MENU_GROUPS } from '../router/menu'

const route = useRoute()
const router = useRouter()

const currentUser = ref(getUser())
const errBar = ref('')
const collapsed = ref(false)

// 页面级错误统一出口：视图经 inject 上报，本壳在内容区顶部展示；切页即清，不泄漏到下一页
const showError = (msg: string) => {
  errBar.value = msg
}
const clearError = () => {
  errBar.value = ''
}
provide('shellError', { errBar, showError, clearError })

const activeMenu = computed(() => route.path)
const header = computed(() => findMenuTitle(route.path))
// 动效容器按 path 重挂载：每次切页重放 page-in（style.css）
const pageKey = computed(() => route.path)

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
    <el-aside :width="collapsed ? '64px' : '228px'" class="layout-aside">
      <div class="aside-brand">
        <span class="brand-mark">OA</span>
        <div v-show="!collapsed" class="aside-brand-text">
          <span class="brand-name">智能办公 Agent</span>
          <span class="brand-sub">Office Automation</span>
        </div>
      </div>
      <el-scrollbar class="aside-scroll">
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
      </el-scrollbar>
    </el-aside>

    <el-container class="layout-body">
      <el-header class="layout-header" height="56px">
        <div class="header-left">
          <el-button text :icon="collapsed ? Expand : Fold" @click="collapsed = !collapsed" />
          <el-breadcrumb separator="/">
            <el-breadcrumb-item v-if="header.group">{{ header.group }}</el-breadcrumb-item>
            <el-breadcrumb-item>
              <span class="header-page">{{ header.title || '智能办公' }}</span>
            </el-breadcrumb-item>
          </el-breadcrumb>
        </div>
        <div class="header-right">
          <el-dropdown trigger="click" @command="doLogout">
            <span class="user-chip">
              <el-avatar :size="24" class="user-avatar">
                <el-icon><User /></el-icon>
              </el-avatar>
              <span class="user-name">{{ currentUser || '已登录' }}</span>
              <el-icon class="user-caret"><ArrowDown /></el-icon>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item :icon="SwitchButton" command="logout">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <el-main class="layout-main">
        <div :key="pageKey" class="page-wrap">
          <el-alert
            v-if="errBar"
            class="page-alert"
            type="error"
            :title="errBar"
            show-icon
            closable
            @close="errBar = ''"
          />
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
  display: flex;
  flex-direction: column;
  background: #fff;
  border-right: 1px solid var(--line);
  overflow: hidden;
  transition: width 0.2s;
}
.aside-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 56px;
  padding: 0 14px;
  border-bottom: 1px solid var(--line);
  white-space: nowrap;
}
.brand-mark {
  flex: none;
  width: 30px;
  height: 30px;
  border-radius: 8px;
  background: linear-gradient(135deg, var(--brand), #4f8ff5);
  color: #fff;
  font-weight: 700;
  font-size: 12px;
  letter-spacing: 0.5px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 2px 6px rgba(31, 111, 235, 0.28);
}
.aside-brand-text {
  display: flex;
  flex-direction: column;
  line-height: 1.25;
  overflow: hidden;
}
.brand-name {
  font-weight: 600;
  font-size: 14px;
}
.brand-sub {
  font-size: 10px;
  color: var(--muted);
  letter-spacing: 0.6px;
  text-transform: uppercase;
}
.aside-scroll {
  flex: 1;
  min-height: 0;
}
.aside-menu {
  border-right: none;
  padding: 8px 8px 20px;
  --el-menu-bg-color: transparent;
  --el-menu-text-color: var(--sub);
  --el-menu-active-color: var(--brand);
  --el-menu-hover-bg-color: var(--brand-soft);
  --el-menu-item-height: 40px;
  --el-menu-sub-item-height: 38px;
}
.aside-menu:not(.el-menu--collapse) {
  width: 100%;
}
.aside-menu :deep(.el-sub-menu__title) {
  font-weight: 600;
  color: var(--fg);
}
.aside-menu :deep(.el-menu-item),
.aside-menu :deep(.el-sub-menu__title) {
  border-radius: 8px;
  margin: 2px 0;
}
/* 选中项：品牌色浅底 + 左侧指示条（比默认纯文字变色更易定位） */
.aside-menu :deep(.el-menu-item.is-active) {
  position: relative;
  background: var(--brand-soft);
  font-weight: 600;
}
.aside-menu :deep(.el-menu-item.is-active)::before {
  content: '';
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 18px;
  border-radius: 0 2px 2px 0;
  background: var(--brand);
}
.layout-body {
  overflow: hidden;
}
.layout-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  background: rgba(255, 255, 255, 0.86);
  border-bottom: 1px solid var(--line);
  backdrop-filter: blur(6px);
}
.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.header-page {
  font-size: 15px;
  font-weight: 600;
}
.header-right {
  display: flex;
  align-items: center;
}
.user-chip {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 4px 10px 4px 4px;
  border-radius: 999px;
  cursor: pointer;
  color: var(--sub);
  transition: background 0.15s ease;
  outline: none;
}
.user-chip:hover {
  background: var(--brand-soft);
}
.user-avatar {
  background: var(--brand-soft);
  color: var(--brand);
}
.user-name {
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.user-caret {
  font-size: 12px;
  color: var(--muted);
}
.layout-main {
  padding: 18px;
  overflow-y: auto;
  background: var(--bg);
}
.page-wrap {
  max-width: var(--content-max);
  margin: 0 auto;
}
.page-alert {
  margin-bottom: 14px;
}
</style>