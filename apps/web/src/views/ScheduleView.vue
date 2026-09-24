<script setup lang="ts">
// 职责：日程与提醒页骨架（PRD §2.2 个人事务智能管理）——仅搭 UI 结构，不接后端接口、零 mock
// 链路：router /schedule → 本页；待办列表复用「我的任务」页，本页管日程/预约/台账/提醒
// 对齐：PRD §2.2 + AGENTS.md §4 前端红线（禁 mock 兜底）
</script>

<template>
  <section class="card">
    <h2>日程与提醒</h2>
    <p class="muted">日程安排、会议预约、工作台账与主动提醒（PRD §2.2）</p>
    <el-tabs>
      <el-tab-pane label="我的日程">
        <div class="toolbar">
          <el-date-picker type="date" placeholder="选择日期" disabled style="width: 160px" />
          <el-button type="primary" disabled>一句话创建日程</el-button>
        </div>
        <el-empty description="功能建设中——日程列表待接入后端接口" />
      </el-tab-pane>

      <el-tab-pane label="会议预约">
        <el-form label-width="88px" style="max-width: 520px">
          <el-form-item label="会议主题">
            <el-input placeholder="如：Q4 产品评审" disabled />
          </el-form-item>
          <el-form-item label="参会人员">
            <el-select placeholder="按姓名搜索" multiple disabled style="width: 100%" />
          </el-form-item>
          <el-form-item label="空闲查询">
            <el-button disabled>查询人员共同空闲</el-button>
            <el-button disabled>预定会议室</el-button>
          </el-form-item>
          <el-form-item label="时间">
            <el-date-picker type="datetimerange" start-placeholder="开始" end-placeholder="结束" disabled style="width: 100%" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" disabled>发起预约并发送邀请</el-button>
          </el-form-item>
        </el-form>
      </el-tab-pane>

      <el-tab-pane label="工作台账">
        <div class="toolbar">
          <el-radio-group model-value="daily" disabled>
            <el-radio-button value="daily">每日汇总</el-radio-button>
            <el-radio-button value="weekly">每周汇总</el-radio-button>
          </el-radio-group>
          <el-button disabled>生成个人工作台账</el-button>
        </div>
        <el-empty description="功能建设中——已完成/待完成梳理待接口" />
      </el-tab-pane>

      <el-tab-pane label="智能提醒">
        <el-table :data="[]">
          <el-table-column prop="type" label="提醒类型" />
          <el-table-column prop="rule" label="触发规则" />
          <el-table-column prop="enabled" label="状态" />
        </el-table>
        <p class="muted">
          覆盖：审批超时预警 / 任务到期提醒 / 会议临近通知 / 项目节点预警；
          定期简报（上班推今日待办、周五提示生成周报草稿）
        </p>
        <el-empty description="功能建设中——提醒配置待接入 notifications 接口" />
      </el-tab-pane>
    </el-tabs>
  </section>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
</style>
