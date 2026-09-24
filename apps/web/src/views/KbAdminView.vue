<script setup lang="ts">
// 职责：知识库后台页骨架（PRD §2.13 系统管理与运营后台·知识库后台）——管理员向 UI，不接接口、零 mock
// 链路：router /kb-admin → 本页；资料入库/切片向量化为后端 RAG 链路（方案 §7.4）
// 对齐：PRD §2.13 + AGENTS.md §4 前端红线（禁 mock 兜底）
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="page-head">
        <span class="card-title">知识库后台</span>
        <span class="head-hint">管理员上传资料、维护知识库、配置回答规则（PRD §2.13）</span>
      </div>
    </template>
    <el-tabs>
      <el-tab-pane label="资料上传">
        <el-upload drag action="#" disabled multiple>
          <div class="upload-hint">支持 Word / PDF / Markdown / Excel 批量上传，自动切片向量化</div>
        </el-upload>
        <el-empty description="功能建设中——入库任务与向量化进度待接口" />
      </el-tab-pane>

      <el-tab-pane label="知识库维护">
        <div class="toolbar">
          <el-select placeholder="选择知识库" disabled style="width: 200px" />
          <el-input placeholder="搜索条目" disabled style="width: 220px" />
          <el-button disabled>新增条目</el-button>
        </div>
        <el-table :data="[]">
          <el-table-column prop="title" label="条目" min-width="200" />
          <el-table-column prop="source" label="来源" width="160" />
          <el-table-column prop="updated" label="更新时间" width="170" />
          <el-table-column prop="perm" label="可见范围" width="140" />
        </el-table>
        <el-empty description="功能建设中——条目清单与权限配置待接口" />
      </el-tab-pane>

      <el-tab-pane label="回答规则">
        <el-form label-width="96px" style="max-width: 520px">
          <el-form-item label="相似度阈值">
            <el-slider :model-value="0.7" disabled />
          </el-form-item>
          <el-form-item label="无答案话术">
            <el-input placeholder="检索不到依据时的统一回复" disabled />
          </el-form-item>
          <el-form-item label="引用展示">
            <el-switch disabled />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" disabled>保存规则</el-button>
          </el-form-item>
        </el-form>
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
.upload-hint {
  color: var(--muted);
  font-size: 13px;
}
</style>
