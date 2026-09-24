<script setup lang="ts">
// 职责：定时任务页骨架（PRD §2.11 高阶自动化·定时任务）——固定办公物料自动产出 UI，不接接口、零 mock
// 链路：router /jobs → 本页；场景串联复用工作流编排页（/workflows），通用自助工具复用工具调试台
// 对齐：PRD §2.11 + AGENTS.md §4 前端红线（禁 mock 兜底）
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="page-head">
        <span class="card-title">定时任务</span>
        <span class="head-hint">自动拉取工作数据、预生成周报、月度台账等固定办公物料（PRD §2.11）</span>
      </div>
    </template>
    <div class="toolbar">
      <el-button type="primary" disabled>新建定时任务</el-button>
      <el-button disabled>从模板创建（周报 / 月度台账）</el-button>
    </div>
    <el-table :data="[]">
      <el-table-column prop="name" label="任务名" min-width="160" />
      <el-table-column prop="cron" label="调度周期" width="150" />
      <el-table-column prop="output" label="产出物料" width="160" />
      <el-table-column prop="last" label="最近执行" width="170" />
      <el-table-column prop="status" label="状态" width="100" />
      <el-table-column label="操作" width="160">
        <template #default>
          <el-button size="small" disabled>立即执行</el-button>
          <el-button size="small" disabled>停用</el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-empty description="功能建设中——定时任务列表与调度待接口" />
  </el-card>
</template>

<style scoped>
.toolbar {
  display: flex;
  gap: 12px;
  margin-bottom: 12px;
}
</style>
