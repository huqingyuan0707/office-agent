// 职责：前端路由表与全局守卫（登录态闸门）
// 链路：main.ts use(router) → beforeEach 校验 token → 懒加载对应视图；/ 重定向 /tools
// 对齐：AGENTS.md §4 前端红线（本文件只做入口闸门，不发接口请求；401 跳转由 api.ts 回调 main.ts 完成）
// 说明：createWebHashHistory —— hash 路由，静态托管零依赖 SPA fallback（无需服务端 rewrite 规则）
import { createRouter, createWebHashHistory, type RouteRecordRaw } from 'vue-router'
import { getToken } from '../api'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    component: () => import('../views/LoginView.vue'),
    meta: { public: true }, // 公开页：无 token 可进、不显示顶栏
  },
  { path: '/', redirect: '/tools' },
  { path: '/tools', component: () => import('../views/ToolsView.vue') },
  { path: '/tasks', component: () => import('../views/TasksView.vue') },
  { path: '/approvals', component: () => import('../views/ApprovalsView.vue') },
  { path: '/agents', component: () => import('../views/AgentsView.vue') },
  { path: '/reports', component: () => import('../views/ReportsView.vue') },
  { path: '/admin', component: () => import('../views/AdminView.vue') },
  { path: '/governance', component: () => import('../views/GovernanceView.vue') },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

// 全局守卫：无 token 且非公开页 → /login；已有 token 访问 /login → /tools
router.beforeEach((to) => {
  if (to.path === '/login') return getToken() ? '/tools' : true
  if (!to.meta.public && !getToken()) return '/login'
  return true
})

export default router
