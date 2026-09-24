<script setup lang="ts">
// 职责：会议协作页骨架（PRD §2.5 会议智能协作）——会前/会中/会后/风险四段式 UI，不接接口、零 mock
// 链路：router /meetings → 本页；功能实现待接 office.meeting.* 工具
// 对齐：PRD §2.5 + AGENTS.md §4 前端红线（禁 mock 兜底）
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="page-head">
        <span class="card-title">会议协作</span>
        <span class="head-hint">覆盖会前准备、会中记录、会后纪要与风险预警全流程（PRD §2.5）</span>
      </div>
    </template>
    <el-steps :active="0" align-center finish-status="wait" class="block-gap">
      <el-step title="会前" description="预约 · 资料 · 议程" />
      <el-step title="会中" description="转录 · 记录 · 重点" />
      <el-step title="会后" description="纪要 · 待办 · 跟进" />
      <el-step title="风险" description="风险点 · 预警" />
    </el-steps>
    <el-tabs>
      <el-tab-pane label="会前准备">
        <div class="toolbar">
          <el-button type="primary" disabled>生成会议议程</el-button>
          <el-button disabled>整理会议资料</el-button>
        </div>
        <el-empty description="功能建设中——待接入 office.meeting.agenda" />
      </el-tab-pane>

      <el-tab-pane label="会中记录">
        <el-alert type="info" :closable="false" title="实时语音转录与自动记录依赖会中通道，暂未开通" class="block-gap" />
        <el-empty description="功能建设中——转录文本将在此滚动展示" />
      </el-tab-pane>

      <el-tab-pane label="会后纪要">
        <div class="toolbar">
          <el-select placeholder="选择会议" disabled style="width: 220px" />
          <el-button type="primary" disabled>生成正式纪要</el-button>
          <el-button disabled>拆解会议待办</el-button>
        </div>
        <el-empty description="功能建设中——纪要与待办分派（责任人/截止时间/跟进）待接口" />
      </el-tab-pane>

      <el-tab-pane label="风险预警">
        <el-empty description="功能建设中——从纪要自动抓取项目风险点待接口（office.meeting.risks）" />
      </el-tab-pane>
    </el-tabs>
  </el-card>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
</style>
