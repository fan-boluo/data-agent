<template>
  <!-- Task Board（跨 Tab 共享） -->
  <div class="task-board" :class="{ collapsed: taskBoardCollapsed }">
    <div class="task-board-header" @click="taskBoardCollapsed = !taskBoardCollapsed">
      <span class="task-board-title">
        <CheckSquareOutlined />
        <span>任务 {{ visibleTasks.length ? '(' + visibleTasks.filter(t => t.status === 'completed').length + '/' + visibleTasks.length + ')' : '' }}</span>
      </span>
      <span class="task-board-toggle">
        <UpOutlined v-if="!taskBoardCollapsed" />
        <DownOutlined v-else />
      </span>
    </div>
    <div v-show="!taskBoardCollapsed" class="task-board-body">
      <div v-if="!visibleTasks.length" class="task-board-empty">暂无任务</div>
      <div
        v-for="task in visibleTasks"
        :key="task.id"
        class="task-chip"
        :class="'task-' + task.status"
      >
        <span class="task-chip-indicator">
          <CheckCircleOutlined v-if="task.status === 'completed'" />
          <LoadingOutlined v-else-if="task.status === 'in_progress'" />
          <ClockCircleOutlined v-else />
        </span>
        <span class="task-chip-subject">{{ task.subject }}</span>
        <span class="task-chip-id">#{{ task.id }}</span>
        <span v-if="task.blockedBy.length" class="task-chip-blocked">{{ task.blockedBy.join(',') }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  CheckCircleOutlined,
  CheckSquareOutlined,
  ClockCircleOutlined,
  DownOutlined,
  LoadingOutlined,
  UpOutlined,
} from '@ant-design/icons-vue'
import type { TaskInfo } from '../composables/useTaskBoard'

// 计划任务看板：chip 列表 + 折叠态（折叠为组件本地交互状态，不外抛）
const props = defineProps<{ tasks: TaskInfo[] }>()

const taskBoardCollapsed = ref(false)
const visibleTasks = computed(() => props.tasks.filter(t => t.status !== 'deleted'))
</script>

<style scoped>
.task-board {
  margin: 0 12px 8px;
  background: var(--bg-page);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-card);
  overflow: hidden;
}

.task-board-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 12px;
  cursor: pointer;
  user-select: none;
  background: var(--bg-subtle);
}

.task-board-header:hover {
  background: var(--bg-hover);
}

.task-board-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}

.task-board-title .anticon {
  font-size: 13px;
  color: var(--accent-primary);
}

.task-board-toggle {
  font-size: 12px;
  color: var(--text-muted);
}

.task-board-body {
  padding: 4px 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 200px;
  overflow-y: auto;
}

.task-chip {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 12px;
  font-size: 12px;
  line-height: 1.6;
  border: 1px solid var(--border-light);
  background: var(--bg-surface);
  color: var(--text-secondary);
}

.task-chip.task-completed {
  opacity: 0.5;
  text-decoration: line-through;
}

.task-chip.task-in_progress {
  border-color: var(--accent-primary);
  color: var(--accent-primary);
}

.task-chip-indicator {
  font-size: 11px;
  flex-shrink: 0;
}

.task-chip.task-completed .task-chip-indicator {
  color: #22c55e;
}

.task-chip.task-in_progress .task-chip-indicator {
  color: var(--accent-primary);
}

.task-chip.task-pending .task-chip-indicator {
  color: var(--text-muted);
}

.task-chip-subject {
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-chip-id {
  font-size: 10px;
  color: var(--text-muted);
  opacity: 0.6;
}

.task-chip-blocked {
  font-size: 10px;
  padding: 0 4px;
  border-radius: 4px;
  background: #fef3c7;
  color: #92400e;
}
</style>
