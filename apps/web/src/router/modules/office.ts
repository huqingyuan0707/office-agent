// 职责：路由+菜单模块「办公办理」——一句话对话发起与任务进度查看
// 链路：menu.ts 聚合 → 侧边栏首组 + /chat、/tasks 两条子路由（挂 MainLayout 下）
// 对齐：PRD §5.1 对话主入口 + frontend-code-style §2（页面按域分目录）
import { ChatDotRound, OfficeBuilding, Tickets } from '@element-plus/icons-vue'
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
  ],
}
