// 职责：路由+菜单模块「流程与审批」——审批复核、工作流编排与定时任务（PRD §2.3/§2.11）
// 链路：menu.ts 聚合 → /approvals、/workflows、/jobs；编排页写步骤落单即停，跳审批页续跑
// 对齐：AGENTS.md §3 写动作恒送审 + PRD §2.11 可视化工作流编排/定时任务
import { AlarmClock, Connection, SetUp, Stamp } from '@element-plus/icons-vue'
import type { MenuGroup } from '../types'

export const processGroup: MenuGroup = {
  key: 'process',
  label: '流程与审批',
  icon: Connection,
  entries: [
    {
      path: '/approvals',
      name: 'approvals',
      component: () => import('../../views/ApprovalsView.vue'),
      meta: { title: '审批中心', icon: Stamp },
    },
    {
      path: '/workflows',
      name: 'workflows',
      component: () => import('../../views/WorkflowsView.vue'),
      meta: { title: '工作流编排', icon: SetUp },
    },
    {
      path: '/jobs',
      name: 'jobs',
      component: () => import('../../views/JobsView.vue'),
      meta: { title: '定时任务', icon: AlarmClock },
    },
  ],
}
