<template>
  <div class="tl-tool" :class="'tool-' + node.status">
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
import type { TraceToolNode } from '@/components/AnalysisTimeline.vue'

const props = defineProps<{
  node: TraceToolNode
  appId?: string
}>()

const expanded = ref(false)

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
