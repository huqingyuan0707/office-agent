// 职责：路由+菜单模块「项目与事务」——项目管理、人事行政与财务辅助（PRD §2.8/§2.9/§2.10）
// 链路：menu.ts 聚合 → /projects、/hr、/finance（骨架页，功能待接后端工具）
// 对齐：PRD §2.9 + §6 任务拆解流程 + §2.8/§2.10
import { Avatar, Briefcase, Money, Suitcase } from '@element-plus/icons-vue'
import type { MenuGroup } from '../types'

export const affairsGroup: MenuGroup = {
  key: 'affairs',
  label: '项目与事务',
  icon: Briefcase,
  entries: [
    {
      path: '/projects',
      name: 'projects',
      component: () => import('../../views/ProjectsView.vue'),
      meta: { title: '项目管理', icon: Suitcase },
    },
    {
      path: '/hr',
      name: 'hr',
      component: () => import('../../views/HrView.vue'),
      meta: { title: '人事行政', icon: Avatar },
    },
    {
      path: '/finance',
      name: 'finance',
      component: () => import('../../views/FinanceView.vue'),
      meta: { title: '财务辅助', icon: Money },
    },
  ],
}
