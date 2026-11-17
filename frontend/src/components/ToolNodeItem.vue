<template>
  <!-- SQL 类工具：复用 SQL 卡片（SQL + 结果预览），不再展示 sql/session_id 参数摘要 -->
  <div v-if="isSqlTool" :id="'trace-step-' + node.id" class="trace-step-wrapper">
    <AnalysisStepCard :step="sqlStep" />
  </div>
  <div v-else class="tl-tool" :class="'tool-' + node.status">
    <div class="tl-tool-head" @click="expanded = !expanded">
      <span class="tl-tool-indicator">
        <LoadingOutlined v-if="node.status === 'running'" spin />
        <CheckCircleOutlined v-else-if="node.status === 'completed'" />
        <CloseCircleOutlined v-else />
      </span>
      <span class="tl-tool-name">{{ node.name }}</span>
      <span class="tl-tool-args">{{ node.argsSummary }}</span>
      <span v-if="node.durationMs != null" class="tl-tool-duration">{{ node.durationMs }}ms</span>
      <span v-if="node.status === 'failed' && node.error" class="tl-tool-error">{{ node.error }}</span>
      <span class="tl-toggle">
        <DownOutlined v-if="expanded" />
        <RightOutlined v-else />
      </span>
    </div>
    <!-- 详情：完整参数 / 结果摘要 / 图表与报告 -->
    <div v-if="expanded" class="tl-tool-body">
      <pre v-if="node.args && Object.keys(node.args).length" class="tl-tool-json">{{ argsJson }}</pre>
      <div v-if="node.render" class="tl-tool-render">{{ node.render }}</div>
      <ToolResultMedia
        :app-id="appId"
        :images="node.images || []"
        :report="node.report || null"
        :chart-option="node.chartOption || null"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  CheckCircleOutlined, CloseCircleOutlined, DownOutlined, LoadingOutlined, RightOutlined,
} from '@ant-design/icons-vue'
import ToolResultMedia from '@/components/ToolResultMedia.vue'
import AnalysisStepCard from '@/components/AnalysisStepCard.vue'
import type { TraceToolNode } from '@/components/AnalysisTimeline.vue'

const props = defineProps<{
  node: TraceToolNode
  appId?: string
}>()

const expanded = ref(false)

// SQL 类工具：Start 参数或 End 事件携带 sql 即按 SQL 卡片渲染
const isSqlTool = computed(() => Boolean(props.node.sql || (props.node.args as any)?.sql))

// 工具节点 → SQL 卡片入参（状态/耗时与工具生命周期一致）
const sqlStep = computed(() => ({
  id: props.node.id,
  type: 'sql' as const,
  title: 'SQL 查询',
  status: props.node.status,
  timestamp: props.node.startTs,
  sql: props.node.sql || String((props.node.args as any)?.sql || ''),
  rowCount: props.node.rowCount,
  durationMs: props.node.durationMs,
  rowsPreview: props.node.rowsPreview || [],
  error: props.node.error,
}))

// 详情里的完整参数（缩进美化）
const argsJson = computed(() => {
  try {
    return JSON.stringify(props.node.args, null, 2)
  } catch {
    return String(props.node.args ?? '')
  }
})
</script>

<style scoped>
/* SQL 卡片容器：支持主对话卡片跳转滚动定位 */
.trace-step-wrapper { scroll-margin-top: 8px; margin-bottom: 8px; }

.tl-tool {
  border: 1px solid var(--border-light, #f0f0f0);
  border-radius: 6px;
  margin-bottom: 6px;
  overflow: hidden;
  background: #fff;
}
.tl-tool.tool-running { border-color: #91caff; background: #f0f5ff; }
.tl-tool.tool-failed { border-color: #ffa39e; background: #fff1f0; }

.tl-tool-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px;
  font-size: 12px;
  cursor: pointer;
  user-select: none;
}
.tl-tool-head:hover { background: var(--bg-hover, #f5f5f5); }

.tl-tool-indicator { font-size: 12px; color: #8c8c8c; flex-shrink: 0; }
.tool-running .tl-tool-indicator { color: #1677ff; }
.tool-completed .tl-tool-indicator { color: #52c41a; }
.tool-failed .tl-tool-indicator { color: #ff4d4f; }

.tl-tool-name { font-weight: 500; color: #262626; flex-shrink: 0; }
.tl-tool-args {
  flex: 1;
  min-width: 0;
  color: #8c8c8c;
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tl-tool-duration { font-size: 11px; color: #8c8c8c; flex-shrink: 0; }
.tl-tool-error {
  font-size: 11px;
  color: #ff4d4f;
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex-shrink: 0;
}
.tl-toggle { font-size: 11px; color: #bfbfbf; flex-shrink: 0; }

.tl-tool-body {
  padding: 6px 8px;
  border-top: 1px solid var(--border-light, #f0f0f0);
}

.tl-tool-json {
  margin: 0 0 6px;
  padding: 6px 8px;
  background: #1e1e1e;
  color: #d4d4d4;
  border-radius: 4px;
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 11px;
  line-height: 1.5;
  overflow-x: auto;
  max-height: 200px;
  white-space: pre;
}

.tl-tool-render {
  font-size: 12px;
  color: #595959;
  margin-bottom: 4px;
}
</style>
