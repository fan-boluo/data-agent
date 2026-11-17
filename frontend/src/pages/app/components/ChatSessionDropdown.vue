<template>
  <!-- Session History Dropdown -->
  <div class="session-dropdown">
    <a-spin :spinning="loading">
      <div v-if="!sessionList.length && !loading" class="session-empty">
        暂无历史会话
      </div>
      <div
        v-for="sess in sessionList"
        :key="sess.session_id"
        class="session-item"
        @click="$emit('load-session', sess.session_id)"
      >
        <div class="session-item-text">{{ sess.first_message }}</div>
      </div>
    </a-spin>
  </div>
</template>

<script setup lang="ts">
import type { SessionListItem } from '../composables/useChatSessions'

// 会话历史下拉：选中项上抛，由页面加载对应会话
defineProps<{
  sessionList: SessionListItem[]
  loading: boolean
}>()

defineEmits<{
  (e: 'load-session', sid: string): void
}>()
</script>

<style scoped>
.session-dropdown {
  background: var(--bg-surface);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-sm);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
  max-height: 200px;
  overflow-y: auto;
  margin: 0 12px 8px;
  padding: 4px 0;
  flex-shrink: 0;
}

.session-item {
  padding: 10px 12px;
  border: 1px solid var(--border-light);
  border-radius: var(--radius-sm);
  margin-bottom: 8px;
  cursor: pointer;
  transition: background 0.15s;
}
.session-item:hover {
  background: var(--bg-hover);
}
.session-item-text {
  font-size: 13px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-empty {
  text-align: center;
  color: var(--text-muted);
  padding: 12px 0;
  font-size: 13px;
}
</style>
