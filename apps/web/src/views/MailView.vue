<script setup lang="ts">
// 职责：邮件与消息页骨架（PRD §2.7 邮件&消息智能处理）——仅搭 UI 结构，不接接口、零 mock
// 链路：router /mail → 本页；功能实现待接邮箱/IM 侧工具
// 对齐：PRD §2.7 + AGENTS.md §4 前端红线（禁 mock 兜底）
</script>

<template>
  <section class="card">
    <h2>邮件与消息</h2>
    <p class="muted">邮箱自动处理、对外邮件预审与群消息定时摘要（PRD §2.7）</p>
    <el-tabs>
      <el-tab-pane label="邮件处理">
        <div class="toolbar">
          <el-select placeholder="邮箱账号" disabled style="width: 200px" />
          <el-button disabled>自动归类</el-button>
          <el-button type="primary" disabled>生成回复草稿</el-button>
        </div>
        <el-table :data="[]">
          <el-table-column prop="subject" label="主题" min-width="200" />
          <el-table-column prop="from" label="发件人" width="160" />
          <el-table-column prop="category" label="归类" width="120" />
          <el-table-column prop="action" label="行动项" width="160" />
        </el-table>
        <el-empty description="功能建设中——邮件列表与行动项转待办待接口" />
      </el-tab-pane>

      <el-tab-pane label="对外邮件预审">
        <el-input type="textarea" placeholder="粘贴拟外发邮件正文，提交前做敏感词 / 泄密 / 语气风险检查" disabled :rows="5" class="block-gap" />
        <el-button type="primary" disabled>预审检查</el-button>
        <el-empty description="功能建设中——预审结果（命中项+位置+建议）待接口" />
      </el-tab-pane>

      <el-tab-pane label="群消息摘要">
        <el-form inline>
          <el-form-item label="群来源">
            <el-select placeholder="飞书 / 企业微信群" multiple disabled style="width: 240px" />
          </el-form-item>
          <el-form-item label="周期">
            <el-select placeholder="定时摘要频率" disabled style="width: 140px">
              <el-option label="每日" value="daily" />
              <el-option label="每周" value="weekly" />
            </el-select>
          </el-form-item>
          <el-form-item>
            <el-button disabled>立即生成摘要</el-button>
          </el-form-item>
        </el-form>
        <el-alert type="info" :closable="false" title="摘要将提炼关键信息并识别 @本人 的任务" class="block-gap" />
        <el-empty description="功能建设中——群消息摘要待接口" />
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
