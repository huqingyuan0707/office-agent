<script setup lang="ts">
// 职责：财务辅助页骨架（PRD §2.10 财务简易辅助）——报销进度/费用台账/预算查询 UI，不接接口、零 mock
// 链路：router /finance → 本页；预算查询待接 office.budget.query
// 对齐：PRD §2.10 + AGENTS.md §4 前端红线（禁 mock 兜底，查询结果必带 source+fetched_at 溯源）
</script>

<template>
  <section class="card">
    <h2>财务辅助</h2>
    <p class="muted">个人报销进度、部门费用台账与项目预算剩余额度查询（PRD §2.10）</p>
    <el-tabs>
      <el-tab-pane label="报销进度">
        <div class="toolbar">
          <el-input placeholder="搜索报销单号 / 事由" disabled style="width: 240px" />
          <el-select placeholder="状态" disabled style="width: 130px">
            <el-option label="审批中" value="pending" />
            <el-option label="已通过" value="approved" />
            <el-option label="已打款" value="paid" />
            <el-option label="已驳回" value="rejected" />
          </el-select>
        </div>
        <el-table :data="[]">
          <el-table-column prop="no" label="单号" width="150" />
          <el-table-column prop="title" label="事由" min-width="180" />
          <el-table-column prop="amount" label="金额" width="110" />
          <el-table-column prop="stage" label="当前节点" width="140" />
        </el-table>
        <el-empty description="功能建设中——报销进度待接口" />
      </el-tab-pane>

      <el-tab-pane label="部门费用台账">
        <div class="toolbar">
          <el-select placeholder="选择部门" disabled style="width: 180px" />
          <el-date-picker type="month" placeholder="统计月份" disabled style="width: 160px" />
          <el-button disabled>简易统计</el-button>
        </div>
        <el-empty description="功能建设中——费用台账统计待接口" />
      </el-tab-pane>

      <el-tab-pane label="预算额度查询">
        <el-form inline>
          <el-form-item label="项目">
            <el-input placeholder="输入项目名查询预算剩余额度" disabled style="width: 280px" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" disabled>查询</el-button>
          </el-form-item>
        </el-form>
        <el-empty description="功能建设中——待接入 office.budget.query（结果带充足/紧张/超支标签与溯源）" />
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
