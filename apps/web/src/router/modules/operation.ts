// 职责：路由+菜单模块「数据与运营」——报表分析、运营总览、系统治理、知识库后台与会话审计（PRD §2.4/§2.13）
// 链路：menu.ts 聚合 → /reports、/admin、/governance、/kb-admin、/audit；admin 页 403 由视图如实提示不造数据
// 对齐：PRD §2.13 系统管理与运营后台 + AGENTS.md §4（一切数据来自后端真实接口）
import {
  Compass,
  DataAnalysis,
  DataLine,
  FolderOpened,
  Histogram,
  View,
} from '@element-plus/icons-vue'
import type { MenuGroup } from '../types'

export const operationGroup: MenuGroup = {
  key: 'operation',
  label: '数据与运营',
  icon: DataLine,
  entries: [
    {
      path: '/reports',
      name: 'reports',
      component: () => import('../../views/ReportsView.vue'),
      meta: { title: '数据报表', icon: DataAnalysis },
    },
    {
      path: '/admin',
      name: 'admin',
      component: () => import('../../views/AdminView.vue'),
      meta: { title: '运营总览', icon: Histogram },
    },
    {
      path: '/governance',
      name: 'governance',
      component: () => import('../../views/GovernanceView.vue'),
      meta: { title: '系统治理', icon: Compass },
    },
    {
      path: '/kb-admin',
      name: 'kb-admin',
      component: () => import('../../views/KbAdminView.vue'),
      meta: { title: '知识库后台', icon: FolderOpened },
    },
    {
      path: '/audit',
      name: 'audit',
      component: () => import('../../views/AuditView.vue'),
      meta: { title: '会话审计', icon: View },
    },
  ],
}
