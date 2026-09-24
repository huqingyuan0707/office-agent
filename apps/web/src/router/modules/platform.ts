// 职责：路由+菜单模块「智能体与工具」——智能体清单运行与工具调试台（管理员向）
// 链路：menu.ts 聚合 → /agents、/tools；工具页走 registry 清单，非白名单不入列
// 对齐：AGENTS.md §3 工具注册一律走 registry + PRD §5.3 工具调试
import { Cpu, MagicStick, Tools } from '@element-plus/icons-vue'
import type { MenuGroup } from '../types'

export const platformGroup: MenuGroup = {
  key: 'platform',
  label: '智能体与工具',
  icon: Cpu,
  entries: [
    {
      path: '/agents',
      name: 'agents',
      component: () => import('../../views/AgentsView.vue'),
      meta: { title: '智能体', icon: MagicStick },
    },
    {
      path: '/tools',
      name: 'tools',
      component: () => import('../../views/ToolsView.vue'),
      meta: { title: '工具调试', icon: Tools },
    },
  ],
}
