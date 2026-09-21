<script setup lang="ts">
// 职责：任务页 —— 任务表格（ID/类型/状态/进度/创建时间），空态与加载态占位
// 链路：router /tasks → api.listTasks；接口失败 → 壳红条 + 列表置空
// 对齐：AGENTS.md §4 前端红线（真实接口零 mock、禁直写 fetch）
import { inject, onMounted, ref } from 'vue'
import { listTasks } from '../api'
import type { TaskItem } from '../api'

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
  <section class="card">
    <h2>
      任务列表
      <span v-if="tasks.length" class="badge">{{ tasks.length }}</span>
    </h2>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>类型</th>
            <th>状态</th>
            <th>进度</th>
            <th>创建时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading">
            <td colspan="5" class="empty">加载中…</td>
          </tr>
          <tr v-else-if="!tasks.length">
            <td colspan="5" class="empty">暂无任务记录</td>
          </tr>
          <template v-else>
            <tr v-for="t in tasks" :key="t.id">
              <td class="mono">{{ t.id }}</td>
              <td>{{ t.type }}</td>
              <td>
                <span class="badge">{{ t.status }}</span>
              </td>
              <td>{{ t.progress }}%</td>
              <td class="muted">{{ t.created_at }}</td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </section>
</template>
