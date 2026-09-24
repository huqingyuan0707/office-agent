// 职责：侧边菜单与路由表的单一来源——聚合各域模块，派生 children 路由与顶栏标题查询
// 链路：modules/*.ts → 本文件 MENU_GROUPS →（MainLayout 渲染 el-menu | router/index.ts 挂 children）
// 对齐：frontend-code-style §2（模块化：新增页面只改对应域模块，壳与路由零改动）
import type { RouteRecordRaw } from 'vue-router'
import { affairsGroup } from './modules/affairs'
import { knowledgeGroup } from './modules/knowledge'
import { officeGroup } from './modules/office'
import { operationGroup } from './modules/operation'
import { platformGroup } from './modules/platform'
import { processGroup } from './modules/process'
import type { MenuGroup } from './types'

export const MENU_GROUPS: MenuGroup[] = [
  officeGroup,
  knowledgeGroup,
  affairsGroup,
  processGroup,
  platformGroup,
  operationGroup,
]

export const menuRoutes: RouteRecordRaw[] = MENU_GROUPS.flatMap((group) => group.entries)

// 顶栏标题回查：按当前路径命中菜单项（非菜单页返回空串，不编造标题）
export const findMenuTitle = (path: string) => {
  for (const group of MENU_GROUPS) {
    const hit = group.entries.find((entry) => entry.path === path)
    if (hit) return { group: group.label, title: hit.meta.title }
  }
  return { group: '', title: '' }
}
