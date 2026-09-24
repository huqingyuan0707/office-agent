<script setup lang="ts">
// 职责：会话审计页骨架（PRD §2.13 系统管理与运营后台·会话审计）——历史对话与敏感告警 UI，不接接口、零 mock
// 链路：router /audit → 本页；审计流后端只追加不改（AGENTS §3），本页仅查询展示
// 对齐：PRD §2.13 + §4.1 权限安全（全程留痕可审计）+ AGENTS.md §4 前端红线
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="page-head">
        <span class="card-title">会话审计</span>
        <span class="head-hint">保存历史对话，敏感操作实时告警（PRD §2.13）</span>
      </div>
    </template>
    <el-tabs>
      <el-tab-pane label="历史会话">
        <div class="toolbar">
          <el-input placeholder="按用户 / 关键词搜索" disabled style="width: 240px" />
          <el-date-picker type="daterange" start-placeholder="开始" end-placeholder="结束" disabled style="width: 260px" />
          <el-button disabled>查询</el-button>
        </div>
        <el-table :data="[]">
          <el-table-column prop="user" label="用户" width="140" />
          <el-table-column prop="goal" label="会话目标" min-width="220" />
          <el-table-column prop="time" label="时间" width="170" />
          <el-table-column prop="trace" label="trace_id" width="180" />
        </el-table>
        <el-empty description="功能建设中——会话历史待接入审计接口" />
      </el-tab-pane>

      <el-tab-pane label="敏感操作告警">
        <div class="toolbar">
          <el-select placeholder="告警级别" disabled style="width: 140px">
            <el-option label="全部" value="all" />
            <el-option label="高危" value="high" />
            <el-option label="提醒" value="info" />
          </el-select>
          <el-button disabled>导出告警记录</el-button>
        </div>
        <el-table :data="[]">
          <el-table-column prop="time" label="时间" width="170" />
          <el-table-column prop="user" label="触发人" width="140" />
          <el-table-column prop="action" label="敏感操作" min-width="200" />
          <el-table-column prop="level" label="级别" width="100" />
        </el-table>
        <el-empty description="功能建设中——实时告警流待接口" />
      </el-tab-pane>
    </el-tabs>
  </el-card>
</template>

<style scoped>
.toolbar {
  display: flex;
  gap: 12px;
  margin-bottom: 12px;
}
</style>
