<script setup lang="ts">
// 职责：人事行政页骨架（PRD §2.8 人事行政资源管理）——考勤/入离职/资源预订 UI，不接接口、零 mock
// 链路：router /hr → 本页；功能实现待接人事行政侧工具
// 对齐：PRD §2.8 + AGENTS.md §4 前端红线（禁 mock 兜底）
</script>

<template>
  <section class="card">
    <h2>人事行政</h2>
    <p class="muted">考勤加班查询、入离职交接与办公资源预订（PRD §2.8）</p>
    <el-tabs>
      <el-tab-pane label="考勤与加班">
        <div class="toolbar">
          <el-date-picker type="month" placeholder="选择月份" disabled style="width: 160px" />
          <el-button disabled>查询考勤 / 加班时长</el-button>
          <el-button type="primary" disabled>自动生成加班申请草稿</el-button>
        </div>
        <el-empty description="功能建设中——考勤数据待接口" />
      </el-tab-pane>

      <el-tab-pane label="入职离职">
        <div class="toolbar">
          <el-select placeholder="选择场景" disabled style="width: 180px">
            <el-option label="入职指引生成" value="onboard" />
            <el-option label="离职交接清单" value="offboard" />
          </el-select>
          <el-button type="primary" disabled>一键生成</el-button>
        </div>
        <el-empty description="功能建设中——指引/清单生成与交接事项提醒待接口" />
      </el-tab-pane>

      <el-tab-pane label="资源预订">
        <el-form inline>
          <el-form-item label="资源类型">
            <el-select placeholder="会议室 / 工位 / 公务车辆" disabled style="width: 180px" />
          </el-form-item>
          <el-form-item label="时间">
            <el-date-picker type="datetimerange" start-placeholder="开始" end-placeholder="结束" disabled style="width: 340px" />
          </el-form-item>
          <el-form-item>
            <el-button disabled>查询占用情况</el-button>
            <el-button type="primary" disabled>预订</el-button>
          </el-form-item>
        </el-form>
        <el-empty description="功能建设中——资源台账与占用查询待接口" />
      </el-tab-pane>
    </el-tabs>
  </section>
</template>

<style scoped>
.toolbar {
  display: flex;
  gap: 12px;
  margin-bottom: 12px;
}
</style>
