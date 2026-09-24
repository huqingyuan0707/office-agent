<script setup lang="ts">
// 职责：知识库问答页骨架（PRD §2.6 企业知识库与增强检索）——仅搭 UI 结构，不接接口、零 mock
// 链路：router /knowledge → 本页；功能实现待接 kb.ask 与跨源检索工具
// 对齐：PRD §2.6 + §4.1 权限适配（仅展示用户可见内容）+ AGENTS.md §4 前端红线
import { Search } from '@element-plus/icons-vue'
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="page-head">
        <span class="card-title">知识库问答</span>
        <span class="head-hint">制度答疑、资料检索与跨源联合检索，结果严格匹配当前用户权限（PRD §2.6）</span>
      </div>
    </template>
    <el-tabs>
      <el-tab-pane label="制度答疑">
        <div class="ask-bar">
          <el-input placeholder="如：年假怎么请？报销额度多少？" disabled>
            <template #append>
              <el-button :icon="Search" disabled>提问</el-button>
            </template>
          </el-input>
        </div>
        <div class="toolbar">
          <el-tag v-for="t in ['考勤', '报销', '人事', '行政', '合规']" :key="t" type="info">{{ t }}</el-tag>
        </div>
        <el-empty description="功能建设中——问答将在此以对话形式展示（kb.ask）" />
      </el-tab-pane>

      <el-tab-pane label="资料检索">
        <el-form inline>
          <el-form-item label="关键词">
            <el-input placeholder="项目资料 / SOP / 历史文档 / 常见问题" disabled style="width: 280px" />
          </el-form-item>
          <el-form-item label="范围">
            <el-select placeholder="全部资料" multiple disabled style="width: 240px" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" disabled>检索</el-button>
          </el-form-item>
        </el-form>
        <el-empty description="功能建设中——检索结果列表待接口" />
      </el-tab-pane>

      <el-tab-pane label="跨源联合检索">
        <el-checkbox-group model-value="" disabled class="block-gap">
          <el-checkbox value="drive">网盘文档</el-checkbox>
          <el-checkbox value="im">聊天记录</el-checkbox>
          <el-checkbox value="oa">OA 单据</el-checkbox>
          <el-checkbox value="pm">项目台账</el-checkbox>
        </el-checkbox-group>
        <el-empty description="功能建设中——一次性多源聚合检索待接口" />
      </el-tab-pane>

      <el-tab-pane label="图片 OCR 问答">
        <el-upload drag action="#" disabled>
          <div class="upload-hint">上传扫描件 / 业务截图，识别内容后就地提问</div>
        </el-upload>
        <el-empty description="功能建设中——待接入 ocr.image" />
      </el-tab-pane>
    </el-tabs>
  </el-card>
</template>

<style scoped>
.ask-bar {
  margin-bottom: 12px;
}
.toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}
.upload-hint {
  color: var(--muted);
  font-size: 13px;
}
</style>
