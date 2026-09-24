<script setup lang="ts">
// 职责：项目管理页骨架（PRD §2.9 项目管理能力 + §6 任务拆解流程）——台账与拆解两步式 UI，不接接口、零 mock
// 链路：router /projects → 本页；拆解实现待接 task.decompose，批量建单走审批闸门
// 对齐：PRD §6.2 用户操作步骤（输入→拆解清单→确认编辑→一键同步）+ §6.5 二次确认约束
</script>

<template>
  <section class="card">
    <h2>项目管理</h2>
    <p class="muted">项目台账查询与一句话任务拆解（PRD §2.9 / §6）</p>
    <el-tabs>
      <el-tab-pane label="项目台账">
        <div class="toolbar">
          <el-input placeholder="搜索项目名 / 责任人" disabled style="width: 240px" />
          <el-select placeholder="状态" disabled style="width: 120px">
            <el-option label="进行中" value="doing" />
            <el-option label="已完成" value="done" />
            <el-option label="有风险" value="risk" />
          </el-select>
          <el-button disabled>生成项目简报</el-button>
        </div>
        <el-table :data="[]">
          <el-table-column prop="name" label="项目" min-width="180" />
          <el-table-column prop="milestone" label="当前里程碑" width="180" />
          <el-table-column prop="risk" label="风险" width="120" />
          <el-table-column prop="owner" label="责任人" width="120" />
        </el-table>
        <el-empty description="功能建设中——台账数据待接口" />
      </el-tab-pane>

      <el-tab-pane label="任务拆解">
        <el-steps :active="1" align-center finish-status="wait" class="block-gap">
          <el-step title="输入主任务" description="目标 / 工期 / 参与人" />
          <el-step title="拆解清单" description="交付物 / 依赖 / 优先级" />
          <el-step title="确认编辑" description="对话微调" />
          <el-step title="一键同步" description="批量建单 + 通知" />
        </el-steps>
        <el-form label-width="72px" class="block-gap">
          <el-form-item label="主任务">
            <el-input type="textarea" placeholder="如：拆解官网改版项目，总共 4 周，参与人：产品、UI、前端、测试" disabled :rows="2" />
          </el-form-item>
          <el-form-item label="参与人">
            <el-select placeholder="缺失时 Agent 会追问，不臆造" multiple disabled style="width: 100%" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" disabled>开始拆解</el-button>
          </el-form-item>
        </el-form>
        <el-table :data="[]">
          <el-table-column prop="task" label="子任务" min-width="160" />
          <el-table-column prop="deliverable" label="交付物" width="140" />
          <el-table-column prop="owner" label="责任人" width="100" />
          <el-table-column prop="due" label="截止时间" width="120" />
          <el-table-column prop="priority" label="优先级" width="90" />
          <el-table-column prop="dep" label="前置依赖" width="140" />
        </el-table>
        <div class="toolbar">
          <el-button disabled>新增子任务</el-button>
          <el-button type="primary" disabled>确认并批量创建（需二次确认）</el-button>
        </div>
        <el-empty description="功能建设中——拆解与批量建单待接口（高危批量操作将弹二次确认）" />
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
