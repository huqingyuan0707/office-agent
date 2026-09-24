<script setup lang="ts">
// 职责：根组件纯路由出口（壳已模块化：业务页在 layouts/MainLayout 侧边栏壳内，登录页整屏独立）
// 链路：main.ts → router → /login 渲染 LoginView；其余命中 MainLayout（壳内 router-view 渲染各域视图）
// 对齐：AGENTS.md §4 前端红线（401 中央处理在 main.ts；壳级错误红条与 shellError 随壳迁至 MainLayout）
</script>

<template>
  <router-view />
</template>
