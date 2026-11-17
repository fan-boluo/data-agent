<template>
  <!-- HITL 审批条：高风险工具执行前贴在输入框上方确认（同一条 SSE 流保持挂起） -->
  <div class="approval-panel">
    <div class="approval-head">
      <span class="approval-risk">
        <ExclamationCircleOutlined />
        需要确认 · 风险等级 {{ pendingApproval.level }}
        <span v-if="pendingApproval.reasons.length">（{{ pendingApproval.reasons.join('、') }}）</span>
      </span>
      <code class="approval-tool">{{ pendingApproval.toolName }}</code>
    </div>
    <div v-if="pendingApproval.title" class="approval-row">{{ pendingApproval.title }}</div>
    <pre v-if="pendingApproval.sql" class="approval-sql">{{ pendingApproval.sql }}</pre>
    <div class="approval-foot">
      <span class="approval-tip">超时（{{ pendingApproval.timeoutSeconds }}s）未处理将自动拒绝</span>
      <div class="approval-actions">
        <a-button size="small" danger :loading="submitting" @click="$emit('resolve', false)">拒绝</a-button>
        <a-button size="small" type="primary" :loading="submitting" @click="$emit('resolve', true)">允许执行</a-button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ExclamationCircleOutlined } from '@ant-design/icons-vue'
import type { PendingApproval } from '../composables/useApproval'

// 审批结果上抛，由页面调用 useApproval.resolveApproval
defineProps<{
  pendingApproval: PendingApproval
  submitting: boolean
}>()

defineEmits<{
  (e: 'resolve', approved: boolean): void
}>()
</script>

<style scoped>
/* HITL 审批条：贴在输入框上方，不遮挡对话内容 */
.approval-panel {
  flex-shrink: 0;
  margin: 0 12px 8px;
  padding: 10px 12px;
  background: #fff7f0;
  border: 1px solid #ffbb96;
  border-radius: var(--radius-card);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
}
.approval-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.approval-risk { color: #d4380d; font-size: 13px; font-weight: 500; }
.approval-tool {
  padding: 1px 6px; border-radius: 4px;
  background: #fff; border: 1px solid #ffd8bf;
  font-size: 12px; color: #ad4e00;
}
.approval-row { margin-bottom: 6px; font-size: 13px; color: #595959; }
.approval-sql {
  max-height: 140px; overflow: auto; margin: 6px 0; padding: 8px;
  background: #fff; border: 1px solid #f0f0f0; border-radius: 6px;
  font-size: 12px; white-space: pre-wrap; word-break: break-all;
}
.approval-foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.approval-tip { font-size: 12px; color: #8c8c8c; }
.approval-actions { display: flex; gap: 8px; }
</style>
