// 职责：路由/菜单共享类型（菜单项即路由记录，侧边栏与路由表同源、零重复登记）
// 链路：modules/*.ts 按此形状声明分组 → menu.ts 聚合派生 → router/index.ts 与 MainLayout 共用
// 对齐：frontend-code-style §1（类型宽松、推断优先；禁 void/Promise<>/Record<> 三词）
import type { Component } from 'vue'
import type { RouteRecordRaw } from 'vue-router'

export type MenuEntry = RouteRecordRaw & { meta: { title: string; icon: Component } }

export interface MenuGroup {
  key: string
  label: string
  icon: Component
  entries: MenuEntry[]
}
