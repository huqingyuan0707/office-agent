<script setup lang="ts">
// 职责：多模态工作台骨架（PRD §2.12 多模态能力·二期）——图片解读与 PPT 生成 UI，不接接口、零 mock
// 链路：router /media → 本页；PPT 生成待接 office.pptx.generate（恒送审）
// 对齐：PRD §2.12 + AGENTS.md §4 前端红线（禁 mock 兜底）
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="page-head">
        <span class="card-title">多模态工作台</span>
        <span class="head-hint">业务截图解读与一键 PPT 生成（PRD §2.12）</span>
      </div>
    </template>
    <el-tabs>
      <el-tab-pane label="图片解读">
        <el-upload drag action="#" disabled>
          <div class="upload-hint">上传业务截图 / 架构图 / 报表图，识别并解读内容</div>
        </el-upload>
        <el-input placeholder="解读追问（可选）：这张报表说明了什么？" disabled class="ops-gap">
          <template #append>
            <el-button disabled>提问</el-button>
          </template>
        </el-input>
        <el-empty description="功能建设中——图片解读结果将在此展示" />
      </el-tab-pane>

      <el-tab-pane label="PPT 生成">
        <el-form label-width="88px" style="max-width: 560px">
          <el-form-item label="内容来源">
            <el-radio-group model-value="doc" disabled>
              <el-radio value="doc">基于文档</el-radio>
              <el-radio value="weekly">基于周报</el-radio>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="大纲">
            <el-input type="textarea" placeholder="每页一行：标题 + 要点（Agent 可代拟）" disabled :rows="6" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" disabled>生成 PPT（写动作·需审批）</el-button>
          </el-form-item>
        </el-form>
        <el-empty description="功能建设中——待接入 office.pptx.generate，批准后产出 .pptx 下载" />
      </el-tab-pane>
    </el-tabs>
  </el-card>
</template>

<style scoped>
.ops-gap {
  margin: 12px 0;
}
.upload-hint {
  color: var(--muted);
  font-size: 13px;
}
</style>
