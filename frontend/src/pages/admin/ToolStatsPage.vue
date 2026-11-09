<template>
  <div id="toolStatsPage" class="page-shell">
    <a-tabs v-model:activeKey="activeTab" class="stats-tabs">
      <a-tab-pane key="tool" tab="工具执行">
        <DailyStatsPanel dimension-label="工具" :fetcher="fetchToolStats" />
      </a-tab-pane>
      <a-tab-pane key="llm" tab="LLM 调用">
        <DailyStatsPanel dimension-label="模型" :fetcher="fetchLlmStats" />
      </a-tab-pane>
    </a-tabs>
  </div>
</template>

<script lang="ts" setup>
import { ref } from 'vue'
import DailyStatsPanel from './components/DailyStatsPanel.vue'
import { getLlmDailyStats, getToolDailyStats } from '@/api/monitorController'
import type { MonitorLlmDailyStats, MonitorToolDailyStats } from '@/types/monitor'

const activeTab = ref('tool')

// 工具执行：telemetry.tool_semantics，按工具名归一化为通用行
const fetchToolStats = async () => {
  const res = await getToolDailyStats({ days: 7 })
  if (res.data.code !== 0 || !res.data.data) throw new Error(res.data.message)
  const data = res.data.data as MonitorToolDailyStats
  return {
    dates: data.dates || [],
    rows: (data.records || []).map((item) => ({
      name: item.toolName,
      date: item.date,
      total: item.total,
      success: item.success,
      failure: item.failure,
    })),
  }
}

// LLM 调用：telemetry.trace_spans（op_type='llm'），按模型名归一化
const fetchLlmStats = async () => {
  const res = await getLlmDailyStats({ days: 7 })
  if (res.data.code !== 0 || !res.data.data) throw new Error(res.data.message)
  const data = res.data.data as MonitorLlmDailyStats
  return {
    dates: data.dates || [],
    rows: (data.records || []).map((item) => ({
      name: item.modelName,
      date: item.date,
      total: item.total,
      success: item.success,
      failure: item.failure,
    })),
  }
}
</script>

<style scoped>
/* 页边距统一由全局 .page-shell 模板类提供（styles/theme.css） */
.stats-tabs :deep(.ant-tabs-nav) {
  margin-bottom: 16px;
}
</style>
