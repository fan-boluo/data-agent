<template>
  <div class="analysis-timeline">
    <div
      v-for="turn in turns"
      :key="turn.id"
      class="trace-turn"
      :class="'turn-' + turn.status"
    >
      <!-- turn 头：请求摘要 + 状态 -->
      <div class="trace-turn-header" @click="toggleTurn(turn)">
        <span class="turn-status-icon">
          <LoadingOutlined v-if="turn.status === 'running'" spin />
          <CheckCircleOutlined v-else-if="turn.status === 'completed'" />
          <CloseCircleOutlined v-else />
        </span>
        <span class="turn-label">{{ turn.label }}</span>
        <span class="turn-time">{{ formatTime(turn.timestamp) }}</span>
        <span class="turn-toggle">
          <UpOutlined v-if="!turn.collapsed" />
          <DownOutlined v-else />
        </span>
      </div>

      <!-- turn 节点列表（时间线纵轨） -->
      <div v-show="!turn.collapsed" class="trace-turn-body">
        <template v-for="node in turn.nodes" :key="node.id">
          <!-- 计划任务节点：DAG 推进状态，子节点为归属的工具调用 -->
          <div v-if="node.kind === 'task'" class="tl-node tl-task" :class="'task-' + node.status">
            <div class="tl-task-head" @click="node.collapsed = !node.collapsed">
              <span class="tl-task-indicator">
                <CheckCircleOutlined v-if="node.status === 'completed'" />
                <LoadingOutlined v-else-if="node.status === 'in_progress'" spin />
                <CloseCircleOutlined v-else-if="node.status === 'failed'" />
                <ClockCircleOutlined v-else />
              </span>
              <span class="tl-task-subject">{{ node.subject }}</span>
              <span class="tl-task-id">#{{ node.taskId }}</span>
              <span class="tl-task-status">{{ taskStatusText(node.status) }}</span>
              <span class="tl-toggle">
                <DownOutlined v-if="!node.collapsed" />
                <RightOutlined v-else />
              </span>
            </div>
            <div v-show="!node.collapsed" class="tl-task-children">
              <template v-for="child in node.children" :key="child.id">
                <ToolNodeItem :node="child" :app-id="appId" />
              </template>
              <div v-if="!node.children.length" class="tl-task-empty">暂无关联工具调用</div>
            </div>
          </div>

          <!-- think 节点：思考阶段，置灰展示 -->
          <div v-else-if="node.kind === 'think'" class="tl-node tl-think">
            <span class="tl-think-icon">🧠</span>
            <span class="tl-think-text">{{ node.text }}</span>
          </div>

          <!-- 分析步骤节点：复用 AnalysisStepCard（SQL/图表/报告） -->
          <div
            v-else-if="node.kind === 'analysis' && findStep(node.stepId)"
            :id="'trace-step-' + node.stepId"
            class="trace-step-wrapper"
          >
            <AnalysisStepCard :step="findStep(node.stepId)!" />
          </div>

          <!-- 工具调用节点（turn 级，无关联任务） -->
          <ToolNodeItem v-else-if="node.kind === 'tool'" :node="node" :app-id="appId" />
        </template>
      </div>
    </div>
  </div>
</template>

<script lang="ts">
// ---- 时间线节点类型（AppChatPage 组装、本组件渲染） ----
export interface ToolReportMeta {
  title?: string
  path?: string
  fileName?: string
  markdown?: string
  createdAt?: number
}

export interface TraceToolNode {
  kind: 'tool'
  id: string
  toolId: string
  name: string
  argsSummary: string
  args?: any
  status: 'running' | 'completed' | 'failed'
  startTs: number
  durationMs?: number
  render?: string
  images?: string[]
  report?: ToolReportMeta | null
  // ECharts 配置（message_type=CHART 的工具产物）
  chartOption?: Record<string, any> | null
  error?: string
  // SQL 类工具卡片数据（ToolExecutionEnd 携带）：ToolNodeItem 据此渲染 SQL 卡片而非参数摘要
  sql?: string
  rowCount?: number
  rowsPreview?: Record<string, any>[]
}

export type TraceTaskStatus = 'pending' | 'in_progress' | 'completed' | 'failed'

export interface TraceTaskNode {
  kind: 'task'
  id: string
  taskId: number
  subject: string
  status: TraceTaskStatus
  collapsed?: boolean
  children: TraceToolNode[]
}

export interface TraceThinkNode {
  kind: 'think'
  id: string
  text: string
  timestamp: number
}

export interface TraceAnalysisNode {
  kind: 'analysis'
  id: string
  stepId: string
}

export type TraceNode = TraceToolNode | TraceTaskNode | TraceThinkNode | TraceAnalysisNode

export interface TraceTurn {
  id: string
  label: string
  status: 'running' | 'completed' | 'failed'
  timestamp: number
  collapsed?: boolean
  nodes: TraceNode[]
}
</script>

<script setup lang="ts">
import { computed } from 'vue'
import {
  CheckCircleOutlined, ClockCircleOutlined, CloseCircleOutlined,
  DownOutlined, LoadingOutlined, RightOutlined, UpOutlined,
} from '@ant-design/icons-vue'
import AnalysisStepCard from '@/components/AnalysisStepCard.vue'
import ToolNodeItem from '@/components/ToolNodeItem.vue'

// 与 AppChatPage 的 AnalysisStep 结构一致（SQL/图表/报告卡片）
interface AnalysisStep {
  id: string
  type: 'sql' | 'chart' | 'report'
  title: string
  status: 'running' | 'completed' | 'failed'
  timestamp: number
}

const props = defineProps<{
  turns: TraceTurn[]
  steps: AnalysisStep[]
  appId?: string
}>()

// 按 stepId 从平面步骤列表解析（SQL/Chart/Report_End 就地替换对象，保持引用最新）
const findStep = (stepId: string) => props.steps.find(s => s.id === stepId)

const toggleTurn = (turn: TraceTurn) => { turn.collapsed = !turn.collapsed }

const formatTime = (ts: number) => {
  const d = new Date(ts)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

const taskStatusText = (s: TraceTaskStatus) => {
  switch (s) {
    case 'in_progress': return '进行中'
    case 'completed': return '已完成'
    case 'failed': return '失败'
    default: return '待开始'
  }
}
</script>

<style scoped>
.analysis-timeline { display: flex; flex-direction: column; }

/* ---- turn 分组 ---- */
.trace-turn {
  border: 1px solid var(--border-light, #e5e7eb);
  border-radius: 6px;
  margin-bottom: 10px;
  background: #fff;
  overflow: hidden;
}
.trace-turn.turn-running { border-color: #91caff; }
.trace-turn.turn-failed { border-color: #ffa39e; }

.trace-turn-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  background: var(--bg-subtle, #fafafa);
  cursor: pointer;
  user-select: none;
  font-size: 13px;
}
.turn-status-icon { font-size: 12px; color: #8c8c8c; }
.turn-running .turn-status-icon { color: #1677ff; }
.turn-completed .turn-status-icon { color: #52c41a; }
.turn-failed .turn-status-icon { color: #ff4d4f; }
.turn-label {
  flex: 1;
  min-width: 0;
  font-weight: 500;
  color: #262626;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.turn-time { font-size: 11px; color: #8c8c8c; flex-shrink: 0; }
.turn-toggle { font-size: 11px; color: #bfbfbf; }

.trace-turn-body {
  padding: 8px 10px 4px 12px;
  border-top: 1px solid var(--border-light, #f0f0f0);
}

/* ---- 节点通用 ---- */
.tl-node { margin-bottom: 8px; }

/* ---- think 节点：置灰 ---- */
.tl-think {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #8c8c8c;
  opacity: 0.8;
}
.tl-think-icon { font-size: 13px; }
.tl-think-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* ---- 计划任务节点 ---- */
.tl-task {
  border: 1px dashed var(--border-color, #d9d9d9);
  border-radius: 6px;
  overflow: hidden;
}
.tl-task-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  background: var(--bg-page, #fafafa);
  cursor: pointer;
  user-select: none;
  font-size: 12px;
}
.tl-task-indicator { font-size: 12px; color: #8c8c8c; }
.tl-task.task-in_progress .tl-task-indicator { color: #1677ff; }
.tl-task.task-completed .tl-task-indicator { color: #52c41a; }
.tl-task.task-failed .tl-task-indicator { color: #ff4d4f; }
.tl-task-subject {
  flex: 1;
  min-width: 0;
  font-weight: 500;
  color: #262626;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tl-task-id { font-size: 10px; color: #8c8c8c; opacity: 0.7; }
.tl-task-status { font-size: 11px; color: #8c8c8c; flex-shrink: 0; }
.tl-task.task-in_progress .tl-task-status { color: #1677ff; }
.tl-task.task-completed .tl-task-status { color: #52c41a; }
.tl-task.task-failed .tl-task-status { color: #ff4d4f; }
.tl-task-children { padding: 6px 8px 2px 18px; border-top: 1px dashed var(--border-light, #f0f0f0); }
.tl-task-empty { font-size: 11px; color: #bfbfbf; padding: 2px 0 4px; }

.trace-step-wrapper { scroll-margin-top: 8px; margin-bottom: 8px; }
</style>
