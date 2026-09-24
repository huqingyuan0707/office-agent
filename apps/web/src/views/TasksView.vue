<script setup lang="ts">
// 职责：任务页 —— 任务表格（ID/类型/状态/进度/创建时间），加载骨架、空态、进度条与状态徽标
// 链路：router /tasks → api.listTasks；接口失败 → 壳红条 + 列表置空
// 对齐：AGENTS.md §4 前端红线（真实接口零 mock、禁直写 fetch、样式 var(--*) token + scoped）
import { inject, onMounted, ref } from 'vue'
import { listTasks } from '../api'
import type { TaskItem } from '../api'
import StatusBadge from '../components/StatusBadge.vue'

const shell = inject('shellError') as {
  showError: (msg: string) => unknown
  clearError: () => unknown
}

const tasks = ref<TaskItem[]>([])
const loading = ref(true)

const loadTasks = async () => {
  loading.value = true
  shell.clearError()
  try {
    tasks.value = await listTasks()
  } catch (e) {
    tasks.value = []
    shell.showError((e as Error).message || '任务列表加载失败')
  } finally {
    loading.value = false
  }
}

onMounted(loadTasks)
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="page-head">
        <span class="card-title">任务列表</span>
        <el-tag v-if="tasks.length" size="small" type="info" round>{{ tasks.length }} 条</el-tag>
      </div>
    </template>

    <el-skeleton v-if="loading" :rows="5" animated />

    <el-table v-else :data="tasks" stripe style="width: 100%">
      <el-table-column prop="id" label="任务 ID" min-width="200">
        <template #default="{ row }">
          <span class="mono">{{ row.id }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="type" label="类型" width="160" />
      <el-table-column label="状态" width="120">
        <template #default="{ row }">
          <StatusBadge :status="row.status" />
        </template>
      </el-table-column>
      <el-table-column label="进度" width="200">
        <template #default="{ row }">
          <el-progress :percentage="Number(row.progress) || 0" :stroke-width="8" />
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="创建时间" min-width="180">
        <template #default="{ row }">
          <span class="muted time">{{ row.created_at }}</span>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="暂无任务记录" :image-size="80" />
      </template>
    </el-table>
  </el-card>
</template>

<style scoped>
.time {
  margin: 0;
}
</style>