<template>
  <!-- 多根片段：逐条渲染消息（外层滚动容器留在页面，保持滚动 ref 归属） -->
  <div v-for="(messageItem, index) in messages" :key="index" class="message-item">
    <div v-if="messageItem.type === 'user'" class="user-message">
      <div class="message-bubble user-bubble">
        {{ messageItem.content }}
      </div>
      <div class="message-avatar">
        <a-avatar :size="28">
          {{ loginUserStore.loginUser.userName?.charAt(0) || 'U' }}
        </a-avatar>
      </div>
    </div>
    <div v-else class="ai-message" :class="{ 'ai-message-failed': messageItem.failed }">
      <div class="message-avatar">
        <a-avatar :src="aiAvatar" :size="28" />
      </div>
      <div class="ai-content">
        <template v-if="messageItem.items?.length">
          <template v-for="(item, ii) in messageItem.items" :key="ii">
            <!-- text：think/中间过程置灰（interim），最终答复正常颜色 -->
            <div v-if="item.type === 'text'" class="text-item" :class="{ 'text-interim': item.interim }">
              <MarkdownRenderer :content="item.text || ''" />
            </div>
            <div v-else-if="item.type === 'step' && item.step && !item.step.sqlCard" class="ai-step-inline" :class="'step-' + item.step.state">
              <span class="step-icon">
                <LoadingOutlined v-if="item.step.state === 'running'" spin />
                <CheckCircleOutlined v-else-if="item.step.state === 'completed'" />
                <CloseCircleOutlined v-else />
              </span>
              <span class="step-desc">{{ item.step.description }}</span>
              <span v-if="item.step.detail" class="step-detail">{{ item.step.detail }}</span>
            </div>
            <div v-else-if="item.type === 'tool' && item.toolResult" class="tool-result-inline">
              <span class="tool-icon"><CodeOutlined /></span>
              <span class="tool-name">{{ item.toolResult.name }}</span>
              <span v-if="item.toolResult.detail" class="tool-detail">{{ item.toolResult.detail }}</span>
            </div>
            <!-- SQL 工具步骤的卡片形态：与普通工具同为一条步骤，点击跳到分析链路对应工具卡片 -->
            <div
              v-else-if="item.type === 'step' && item.step && item.step.sqlCard"
              class="sql-card-message"
              :class="{ 'sql-failed': !item.step.sqlCard.success }"
              @click="$emit('jump-trace', item.step.sqlCard.stepId)"
            >
              <span class="sql-card-icon">🗄️</span>
              <div class="sql-card-body">
                <div class="sql-card-title">
                  SQL 查询
                  <span v-if="item.step.sqlCard.success" class="sql-card-meta">
                    {{ item.step.sqlCard.rowCount ?? 0 }} 行 · {{ item.step.sqlCard.durationMs ?? 0 }}ms
                  </span>
                  <span v-else class="sql-card-meta sql-card-meta-fail">执行失败</span>
                </div>
                <div class="sql-card-snippet">{{ item.step.sqlCard.snippet }}</div>
              </div>
              <span class="sql-card-jump">→</span>
            </div>
            <!-- media：工具产物联动（ECharts 图表 + 普通图片 + 报告查看/下载） -->
            <ToolResultMedia
              v-else-if="item.type === 'media' && item.media"
              :app-id="appId"
              :images="item.media.images"
              :report="item.media.report"
              :chart-option="item.media.chartOption || null"
            />
          </template>
        </template>
        <template v-else>
          <MarkdownRenderer v-if="messageItem.content" :content="messageItem.content" />
          <div v-if="messageItem.steps?.length" class="ai-steps">
            <div v-for="(step, si) in messageItem.steps" :key="si" class="ai-step" :class="'step-' + step.state">
              <span class="step-icon">
                <LoadingOutlined v-if="step.state === 'running'" spin />
                <CheckCircleOutlined v-else-if="step.state === 'completed'" />
                <CloseCircleOutlined v-else />
              </span>
              <span class="step-desc">{{ step.description }}</span>
              <span v-if="step.detail" class="step-detail">{{ step.detail }}</span>
            </div>
          </div>
        </template>
        <div v-if="messageItem.loading" class="loading-indicator">
          <a-spin size="small" />
          <span>AI 正在思考...</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useLoginUserStore } from '@/stores/loginUser'
import MarkdownRenderer from '@/components/MarkdownRenderer.vue'
import ToolResultMedia from '@/components/ToolResultMedia.vue'
import type { MessageItem } from '../composables/useChatMessages'
import aiAvatar from '@/assets/aiAvatar.png'

// 单条消息渲染：气泡 / items（text/step/tool/sql-card/media）/ steps / loading
defineProps<{
  messages: MessageItem[]
  appId?: string
}>()

// 点击 SQL 卡片跳到分析链路对应步骤（由页面转发到时间线域）
defineEmits<{
  (e: 'jump-trace', stepId: string): void
}>()

const loginUserStore = useLoginUserStore()
</script>

<style scoped>
.message-item {
  margin-bottom: 4px;
}

.user-message {
  display: flex;
  justify-content: flex-end;
  align-items: flex-end;
  gap: 8px;
}

.ai-message {
  display: flex;
  justify-content: flex-start;
  align-items: flex-start;
  gap: 8px;
}

.ai-content {
  max-width: 85%;
  min-width: 0;
}

.ai-content > * {
  margin-bottom: 4px;
}

.message-bubble {
  padding: 10px 16px;
  border-radius: var(--radius-card);
  line-height: 1.6;
  font-size: 14px;
  transition: all 0.2s ease;
}

.user-bubble {
  max-width: 80%;
  background: var(--accent-primary);
  border: none;
  color: var(--text-inverse);
  border-bottom-right-radius: 4px;
  box-shadow: var(--shadow-sm);
}

.message-avatar {
  flex-shrink: 0;
}

/* 失败态消息：红色左边框 + 浅红底，醒目标识生成中断（保留已流式内容） */
.ai-message-failed .ai-content {
  padding: 6px 10px;
  border-left: 3px solid #ef4444;
  background: #fff1f0;
  border-radius: 0 var(--radius-card) var(--radius-card) 0;
}

.message-avatar :deep(.ant-avatar) {
  border: 1px solid var(--border-light);
  border-radius: 50%;
}

.loading-indicator {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-muted);
}

.ai-steps {
  margin-top: 6px;
}

.ai-step {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.5;
}

.ai-step .step-icon {
  flex-shrink: 0;
  font-size: 12px;
  width: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.ai-step.step-running .step-icon {
  color: var(--accent-primary);
}

.ai-step.step-completed .step-icon {
  color: #22c55e;
}

.ai-step.step-failed .step-icon {
  color: #ef4444;
}

.ai-step.step-running .step-desc {
  color: var(--accent-primary);
}

.ai-step .step-detail {
  color: var(--text-muted);
  opacity: 0.7;
  margin-left: auto;
  font-size: 11px;
}

.ai-step-inline {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.5;
  margin: 2px 0;
  opacity: 0.7;
}

.ai-step-inline .step-icon {
  flex-shrink: 0;
  font-size: 12px;
  width: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.ai-step-inline.step-running .step-icon {
  color: var(--accent-primary);
}

.ai-step-inline.step-completed .step-icon {
  color: #22c55e;
}

.ai-step-inline.step-failed .step-icon {
  color: #ef4444;
}

.ai-step-inline.step-running .step-desc {
  color: var(--accent-primary);
}

.ai-step-inline .step-detail {
  color: var(--text-muted);
  opacity: 0.7;
  margin-left: auto;
  font-size: 11px;
}

/* Tool result inline in history */
.tool-result-inline {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 2px 6px;
  margin: 2px 0;
  border-radius: 4px;
  background: var(--bg-page);
  font-size: 11px;
  color: var(--text-muted);
}
.tool-result-inline .tool-icon {
  font-size: 11px;
  color: var(--accent-primary);
  flex-shrink: 0;
}
.tool-result-inline .tool-name {
  font-weight: 500;
  color: var(--text-secondary);
  flex-shrink: 0;
}
.tool-result-inline .tool-detail {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* think/中间过程文本置灰，最终答复（RequestCompleted 后）恢复正文颜色 */
.text-item.text-interim :deep(.markdown-content) {
  color: var(--text-muted, #8c8c8c);
  opacity: 0.8;
}

/* ---- SQL 卡片（穿插在 AI 消息 items 里） ---- */
.sql-card-message {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  margin: 6px 0;
  background: #f6f9ff;
  border: 1px solid #d6e4ff;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.2s;
}
.sql-card-message:hover { background: #eaf1ff; border-color: #91caff; box-shadow: 0 2px 6px rgba(22, 119, 255, 0.1); }
.sql-card-message.sql-failed { background: #fff1f0; border-color: #ffa39e; }
.sql-card-message.sql-failed:hover { background: #ffe7e5; border-color: #ff7875; }

.sql-card-icon { font-size: 18px; flex-shrink: 0; }
.sql-card-body { flex: 1; min-width: 0; }
.sql-card-title {
  font-size: 12px;
  font-weight: 600;
  color: #262626;
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 2px;
}
.sql-card-meta { font-size: 11px; color: #1677ff; font-weight: 400; }
.sql-card-meta-fail { color: #ff4d4f; }
.sql-card-snippet {
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 11px;
  color: #595959;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sql-card-jump { color: #1677ff; font-size: 14px; flex-shrink: 0; }

@media (max-width: 768px) {
  .message-bubble {
    max-width: 90%;
  }
}
</style>
