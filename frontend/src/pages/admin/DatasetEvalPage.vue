<template>
  <div id="datasetEvalPage" class="page-shell">
    <div class="page-header">
      <div class="subtle-text">标准题集回归评测：数据集概览与历史运行结果</div>
      <a-button :loading="loading" @click="fetchAll">刷新</a-button>
    </div>

    <a-card class="panel-card" title="评测题集">
      <a-table
        row-key="id"
        size="small"
        :loading="datasetLoading"
        :columns="datasetColumns"
        :data-source="datasets"
        :pagination="false"
        :scroll="{ x: 900 }"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.dataIndex === 'created_at'">
            {{ formatDateTime(record.created_at) }}
          </template>
        </template>
      </a-table>
    </a-card>

    <a-card class="panel-card" title="评测运行">
      <a-table
        row-key="id"
        size="small"
        :loading="runLoading"
        :columns="runColumns"
        :data-source="runs"
        :pagination="false"
        :scroll="{ x: 1200 }"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.dataIndex === 'status'">
            <a-tag :color="statusColor(record.status)">{{ statusLabel(record.status) }}</a-tag>
          </template>
          <template v-else-if="column.dataIndex === 'progress'">
            {{ record.case_done }}/{{ record.case_total }}
            <span v-if="record.case_failed" class="failed-text">（失败 {{ record.case_failed }}）</span>
          </template>
          <template v-else-if="column.dataIndex === 'ex'">
            {{ formatPercent(record.metrics?.ex) }}
          </template>
          <template v-else-if="column.dataIndex === 'ves'">
            {{ formatScore(record.metrics?.ves) }}
          </template>
          <template v-else-if="column.dataIndex === 'valid_sql'">
            {{ formatPercent(record.metrics?.valid_sql) }}
          </template>
          <template v-else-if="column.dataIndex === 'tool_success_rate'">
            {{ formatPercent(record.metrics?.tool_success_rate) }}
          </template>
          <template v-else-if="column.dataIndex === 'avg_duration_ms'">
            {{ formatDuration(record.metrics?.avg_duration_ms) }}
          </template>
          <template v-else-if="column.dataIndex === 'dataset_id'">
            {{ datasetNames[record.dataset_id] || record.dataset_id }}
          </template>
          <template v-else-if="column.dataIndex === 'created_at'">
            {{ formatDateTime(record.created_at) }}
          </template>
        </template>
      </a-table>
    </a-card>
  </div>
</template>

<script lang="ts" setup>
import { computed, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import dayjs from 'dayjs'
import { listEvalDatasets, listEvalRuns } from '@/api/evalController'
import type { EvalDataset, EvalRun } from '@/types/eval'

const loading = ref(false)
const datasetLoading = ref(false)
const runLoading = ref(false)
const datasets = ref<EvalDataset[]>([])
const runs = ref<EvalRun[]>([])

const datasetColumns = [
  { title: '题集', dataIndex: 'name', width: 180 },
  { title: '说明', dataIndex: 'description' },
  { title: '题目数', dataIndex: 'case_total', width: 100 },
  { title: '已校验', dataIndex: 'case_verified', width: 100 },
  { title: '创建时间', dataIndex: 'created_at', width: 180 },
]

const runColumns = [
  { title: '运行名', dataIndex: 'name', width: 220 },
  { title: '题集', dataIndex: 'dataset_id', width: 120 },
  { title: '状态', dataIndex: 'status', width: 100 },
  { title: '进度', dataIndex: 'progress', width: 140 },
  { title: 'EX', dataIndex: 'ex', width: 90 },
  { title: 'VES', dataIndex: 'ves', width: 90 },
  { title: 'SQL 有效率', dataIndex: 'valid_sql', width: 110 },
  { title: '工具成功率', dataIndex: 'tool_success_rate', width: 110 },
  { title: '平均耗时', dataIndex: 'avg_duration_ms', width: 110 },
  { title: '创建时间', dataIndex: 'created_at', width: 180 },
]

// 运行行只存 dataset_id，展示时映射为题集名
const datasetNames = computed<Record<number, string>>(() =>
  Object.fromEntries(datasets.value.map((item) => [item.id, item.name]))
)

const fetchAll = async () => {
  loading.value = true
  await Promise.all([fetchDatasets(), fetchRuns()])
  loading.value = false
}

const fetchDatasets = async () => {
  datasetLoading.value = true
  try {
    const res = await listEvalDatasets()
    if (res.data.code === 0) {
      datasets.value = res.data.data || []
      return
    }
    message.error(`获取题集失败：${res.data.message}`)
  } catch {
    message.error('获取题集失败')
  } finally {
    datasetLoading.value = false
  }
}

const fetchRuns = async () => {
  runLoading.value = true
  try {
    const res = await listEvalRuns({ limit: 50 })
    if (res.data.code === 0) {
      runs.value = res.data.data || []
      return
    }
    message.error(`获取评测运行失败：${res.data.message}`)
  } catch {
    message.error('获取评测运行失败')
  } finally {
    runLoading.value = false
  }
}

const statusLabel = (status?: string) =>
  ({
    pending: '等待中',
    running: '运行中',
    completed: '已完成',
    failed: '失败',
    cancelled: '已取消',
  } as Record<string, string>)[status || ''] ||
  status ||
  '-'

const statusColor = (status?: string) =>
  ({
    pending: 'default',
    running: 'blue',
    completed: 'green',
    failed: 'red',
    cancelled: 'orange',
  } as Record<string, string>)[status || ''] || 'default'

const formatPercent = (value?: number | null) =>
  value == null ? '-' : `${(Number(value) * 100).toFixed(1)}%`

const formatScore = (value?: number | null) =>
  value == null ? '-' : Number(value).toFixed(3)

// 平均耗时字段为毫秒，统一按秒展示
const formatDuration = (value?: number | null) =>
  value == null ? '-' : `${(Number(value) / 1000).toFixed(2)}s`

const formatDateTime = (value?: string) => (value ? dayjs(value).format('YYYY-MM-DD HH:mm:ss') : '-')

onMounted(() => {
  fetchAll()
})
</script>

<style scoped>
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}

.panel-card {
  margin-bottom: 16px;
}

.subtle-text {
  color: rgba(0, 0, 0, 0.45);
  font-size: 12px;
}

.failed-text {
  color: #ff4d4f;
}
</style>
