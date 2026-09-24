// 职责：路由+菜单模块「数据与运营」——报表自助分析、运营总览（仅 admin）与系统治理面板
// 链路：menu.ts 聚合 → /reports、/admin、/governance；admin 页 403 由视图如实提示不造数据
// 对齐：PRD §5.3 批次 B 前端两页 + AGENTS.md §4（一切数据来自后端真实接口）
import { Compass, DataAnalysis, DataLine, Histogram } from '@element-plus/icons-vue'
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
  ],
}
