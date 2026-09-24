// 职责：路由+菜单模块「知识与协作」——知识库问答、邮件消息与多模态工作台（PRD §2.6/§2.7/§2.12）
// 链路：menu.ts 聚合 → /knowledge、/mail、/media（骨架页，功能待接后端工具）
// 对齐：PRD §2.6 知识库&增强检索 + §2.7 邮件&消息 + §2.12 多模态
import { Collection, Message, Picture, Reading } from '@element-plus/icons-vue'
import type { MenuGroup } from '../types'

export const knowledgeGroup: MenuGroup = {
  key: 'knowledge',
  label: '知识与协作',
  icon: Collection,
  entries: [
    {
      path: '/knowledge',
      name: 'knowledge',
      component: () => import('../../views/KnowledgeView.vue'),
      meta: { title: '知识库问答', icon: Reading },
    },
    {
      path: '/mail',
      name: 'mail',
      component: () => import('../../views/MailView.vue'),
      meta: { title: '邮件与消息', icon: Message },
    },
    {
      path: '/media',
      name: 'media',
      component: () => import('../../views/MediaView.vue'),
      meta: { title: '多模态工作台', icon: Picture },
    },
  ],
}
