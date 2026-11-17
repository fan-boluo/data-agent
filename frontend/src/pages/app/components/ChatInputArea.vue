<template>
  <!-- 输入框（跨 Tab 共享）：输入/数据集选择/发送与停止 -->
  <div class="input-container" :style="{ height: inputHeight + 'px' }">
    <div class="resizer-horizontal input-resizer" @mousedown="$emit('resize-start', $event)" />
    <div class="input-wrapper">
      <a-textarea
        :value="modelValue"
        :placeholder="placeholder"
        :maxlength="1000"
        @keydown.enter.prevent="$emit('send')"
        @update:value="$emit('update:modelValue', $event)"
        :disabled="generating || !canOperate"
        class="chat-input"
      />
      <!-- 数据集选择：默认未选择（agent 分不清时会反问） -->
      <div v-if="datasets.length" class="input-toolbar">
        <a-select
          :value="currentDb"
          :options="datasets"
          :placeholder="'未选择数据集'"
          :disabled="generating || !canOperate"
          allow-clear
          size="small"
          class="dataset-select"
          @update:value="onCurrentDbChange"
        />
      </div>
      <div class="input-actions">
        <a-button v-if="generating" danger @click="$emit('stop')" :loading="stopping" size="small">
          <template #icon><StopOutlined /></template>
        </a-button>
        <a-button v-else type="primary" @click="$emit('send')" :disabled="!canOperate" size="small">
          <template #icon><SendOutlined /></template>
        </a-button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { SendOutlined, StopOutlined } from '@ant-design/icons-vue'

// 输入区纯展示组件：状态经 v-model 双向绑定回页面（userInput / currentDb）
defineProps<{
  modelValue: string
  currentDb?: string
  datasets: Array<{ value: string; label: string }>
  generating: boolean
  stopping: boolean
  canOperate: boolean
  inputHeight: number
  placeholder: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
  (e: 'update:currentDb', value: string | undefined): void
  (e: 'send'): void
  (e: 'stop'): void
  (e: 'resize-start', event: MouseEvent): void
}>()

// allow-clear 清空时值为 undefined
const onCurrentDbChange = (value: string | undefined) => {
  emit('update:currentDb', value)
}
</script>

<style scoped>
.input-container {
  display: flex;
  flex-direction: column;
  padding: 0 12px 12px;
  background: var(--bg-surface);
  flex-shrink: 0;
}

.input-resizer {
  cursor: row-resize;
  height: 4px;
  flex-shrink: 0;
  margin-bottom: 4px;
}

.input-resizer:hover,
.input-resizer:active {
  background: var(--accent-primary);
  border-radius: 2px;
}

.input-wrapper {
  position: relative;
  flex: 1;
  display: flex;
}

.chat-input {
  background: var(--bg-page);
  border: 1px solid var(--border-light);
  color: var(--text-primary);
  font-size: 14px;
  border-radius: var(--radius-card);
  transition: all 0.2s ease;
  resize: none;
  width: 100%;
}
.chat-input:hover {
  border-color: var(--border-deep);
}
.chat-input:focus {
  border-color: var(--accent-primary);
  box-shadow: 0 0 0 3px var(--accent-primary-light);
}

.chat-input :deep(textarea) {
  resize: vertical;
  min-height: 60px;
  padding-bottom: 36px;
}

.input-actions {
  position: absolute;
  bottom: 8px;
  right: 8px;
  display: flex;
  gap: 8px;
}

/* 数据集选择器（输入框底部左侧，与发送按钮同行） */
.input-toolbar {
  position: absolute;
  left: 8px;
  bottom: 8px;
  display: flex;
  align-items: center;
  max-width: calc(100% - 64px);
}

.dataset-select {
  min-width: 170px;
  max-width: 100%;
}

.dataset-select :deep(.ant-select-selector) {
  border-radius: 999px;
  background: var(--bg-surface);
}

/* Send button */
.input-actions :deep(.ant-btn-primary) {
  border-radius: 999px;
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
}
</style>
