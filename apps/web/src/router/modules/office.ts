// 职责：路由+菜单模块「办公办理」——对话/任务/文档/日程/会议等员工日常提效入口（PRD §2.1/§2.2/§2.5）
// 链路：menu.ts 聚合 → 侧边栏首组 + /chat、/tasks、/docs、/schedule、/meetings（挂 MainLayout 下）
// 对齐：PRD §5.1 对话主入口 + frontend-code-style §2（页面按域分目录）
import {
  Calendar,
  ChatDotRound,
  Document,
  Notebook,
  OfficeBuilding,
  Tickets,
} from '@element-plus/icons-vue'
import type { MenuGroup } from '../types'

export const officeGroup: MenuGroup = {
  key: 'office',
  label: '办公办理',
  icon: OfficeBuilding,
  entries: [
    {
      path: '/chat',
      name: 'chat',
      component: () => import('../../views/ChatView.vue'),
      meta: { title: '对话办理', icon: ChatDotRound },
    },
    {
      path: '/tasks',
      name: 'tasks',
      component: () => import('../../views/TasksView.vue'),
      meta: { title: '我的任务', icon: Tickets },
    },
    {
      path: '/docs',
      name: 'docs',
      component: () => import('../../views/DocGenView.vue'),
      meta: { title: '文档中心', icon: Document },
    },
    {
      path: '/schedule',
      name: 'schedule',
      component: () => import('../../views/ScheduleView.vue'),
      meta: { title: '日程与提醒', icon: Calendar },
    },
    {
      path: '/meetings',
      name: 'meetings',
      component: () => import('../../views/MeetingView.vue'),
      meta: { title: '会议协作', icon: Notebook },
    },
  ],
}
