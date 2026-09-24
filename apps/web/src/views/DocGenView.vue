<script setup lang="ts">
// 职责：文档中心页骨架（PRD §2.1 智能内容生成与文档处理）——仅搭 UI 结构，不接后端接口、零 mock
// 链路：router /docs → 本页；功能实现待接 office.* 工具接口后替换各页签空态
// 对齐：PRD §2.1 + AGENTS.md §4 前端红线（禁 mock 兜底，骨架页只放结构与「建设中」空态）
</script>

<template>
  <section class="card">
    <h2>文档中心</h2>
    <p class="muted">周报、纪要、邮件等内容生成与文档处理统一入口（PRD §2.1）</p>
    <el-tabs>
      <el-tab-pane label="自动生成">
        <el-form inline label-width="72px">
          <el-form-item label="文档类型">
            <el-select placeholder="选择类型" disabled style="width: 160px">
              <el-option label="周报" value="weekly" />
              <el-option label="月报" value="monthly" />
              <el-option label="工作总结" value="summary" />
              <el-option label="会议纪要" value="minutes" />
              <el-option label="工作汇报" value="report" />
              <el-option label="通知公告" value="notice" />
              <el-option label="邮件" value="email" />
              <el-option label="方案草稿" value="proposal" />
            </el-select>
          </el-form-item>
          <el-form-item label="时间范围">
            <el-date-picker type="week" placeholder="选择周期" disabled style="width: 180px" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" disabled>生成草稿</el-button>
          </el-form-item>
        </el-form>
        <el-input type="textarea" placeholder="补充要点（可选）：本周做了什么、下周计划…" disabled :rows="3" class="block-gap" />
        <el-empty description="功能建设中——待接入后端生成接口" />
      </el-tab-pane>

      <el-tab-pane label="文档处理">
        <el-radio-group model-value="digest" disabled>
          <el-radio-button value="digest">长文摘要</el-radio-button>
          <el-radio-button value="keypoints">重点提炼</el-radio-button>
          <el-radio-button value="merge">多文档整合</el-radio-button>
          <el-radio-button value="polish">润色改写</el-radio-button>
          <el-radio-button value="format">格式统一</el-radio-button>
        </el-radio-group>
        <el-input type="textarea" placeholder="粘贴或上传待处理文本" disabled :rows="4" class="ops-gap" />
        <el-empty description="功能建设中——待接入后端处理接口" />
      </el-tab-pane>

      <el-tab-pane label="文件解析">
        <el-upload drag action="#" disabled>
          <div class="upload-hint">拖拽或点击上传 Word / PDF / Excel 文件</div>
        </el-upload>
        <el-empty description="功能建设中——待接入 office.docx / xlsx 解析接口" />
      </el-tab-pane>

      <el-tab-pane label="文档对比">
        <div class="grid2">
          <el-input placeholder="基准版本文档" disabled>
            <template #prepend>版本 A</template>
          </el-input>
          <el-input placeholder="对比版本文档" disabled>
            <template #prepend>版本 B</template>
          </el-input>
        </div>
        <el-empty description="功能建设中——差异识别与版本变更摘要待接口" />
      </el-tab-pane>

      <el-tab-pane label="批量处理">
        <el-checkbox-group model-value="" disabled>
          <el-checkbox value="pdf">批量转 PDF</el-checkbox>
          <el-checkbox value="rename">批量重命名</el-checkbox>
          <el-checkbox value="image">批量提取图片</el-checkbox>
          <el-checkbox value="digest">批量摘要</el-checkbox>
        </el-checkbox-group>
        <el-empty description="功能建设中——批量任务队列待接口" />
      </el-tab-pane>

      <el-tab-pane label="自定义模板">
        <div class="grid2">
          <div>
            <p class="muted">保存常用格式（周报、请假说明等）供 Agent 复用</p>
            <el-button type="primary" disabled>新建模板</el-button>
          </div>
          <el-empty description="暂无模板——列表待接入 template.list" :image-size="60" />
        </div>
      </el-tab-pane>

      <el-tab-pane label="术语翻译">
        <div class="grid2">
          <el-select placeholder="源语言" disabled style="width: 140px" />
          <el-select placeholder="目标语言" disabled style="width: 140px" />
        </div>
        <el-input placeholder="输入待翻译文本，专业名词按内部术语库统一" disabled class="ops-gap" />
        <el-empty description="功能建设中——术语库与翻译待接口" />
      </el-tab-pane>
    </el-tabs>
  </section>
</template>

<style scoped>
.ops-gap {
  margin: 12px 0;
}
.grid2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-bottom: 12px;
}
.upload-hint {
  color: var(--muted);
  font-size: 13px;
}
</style>
