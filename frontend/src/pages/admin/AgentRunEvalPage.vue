<template>
  <div id="agentRunEvalPage" class="page-shell">
    <div class="page-header">
      <div class="subtle-text">
        基于真实运行数据的 Agent 表现评估：轮次、耗时、工具调用与错误分布
      </div>
      <a-button :loading="loading" @click="fetchSessions">刷新</a-button>
    </div>

    <a-card class="panel-card">
      <a-form layout="inline" :model="query" class="search-form">
        <a-form-item label="应用">
          <a-input v-model:value="query.appId" placeholder="输入 appId" allow-clear />
        </a-form-item>
        <a-form-item label="用户">
          <a-input v-model:value="query.userId" placeholder="输入 userId" allow-clear />
        </a-form-item>
        <a-form-item label="状态">
          <a-select v-model:value="query.status" allow-clear style="width: 140px" placeholder="全部状态">
            <a-select-option value="running">运行中</a-select-option>
            <a-select-option value="completed">已完成</a-select-option>
            <a-select-option value="stopped">已停止</a-select-option>
            <a-select-option value="failed">失败</a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item>
          <a-space>
            <a-button type="primary" @click="doSearch">搜索</a-button>
            <a-button @click="resetSearch">重置</a-button>
          </a-space>
        </a-form-item>
      </a-form>

      <a-divider />
      <a-table
        row-key="sessionId"
        :loading="loading"
        :columns="columns"
        :data-source="pageData.records"
        :pagination="pagination"
        :scroll="{ x: 1300 }"
        @change="onTableChange"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.dataIndex === 'status'">
            <a-tag :color="statusColor(record.status)">{{ statusLabel(record.status) }}</a-tag>
          </template>
          <template v-else-if="column.dataIndex === 'durationMs'">
            {{ formatDuration(record.durationMs) }}
          </template>
          <template v-else-if="column.dataIndex === 'avgLlmLatencyMs'">
            {{ formatDuration(record.avgLlmLatencyMs) }}
          </template>
          <template v-else-if="column.dataIndex === 'startedAt'">
            {{ formatDateTime(record.startedAt) }}
          </template>
        </template>
      </a-table>
    </a-card>
  </div>
</template>

<script lang="ts" setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import dayjs from 'dayjs'
import { listMonitorSessions } from '@/api/monitorController'
import type { MonitorPageData, MonitorSessionSummary } from '@/types/monitor'

const loading = ref(false)
const pageData = ref<MonitorPageData<MonitorSessionSummary>>({
  records: [],
  pageNumber: 1,
  pageSize: 10,
  totalPage: 0,
  totalRow: 0,
})

const query = reactive({
  appId: '',
  userId: '',
  status: undefined as string | undefined,
})

const columns = [
  { title: 'Session ID', dataIndex: 'sessionId', width: 260 },
  { title: '应用', dataIndex: 'appId', width: 120 },
  { title: '用户', dataIndex: 'userId', width: 120 },
  { title: '状态', dataIndex: 'status', width: 100 },
  { title: '轮次', dataIndex: 'totalTurns', width: 80 },
  { title: '运行耗时', dataIndex: 'durationMs', width: 110 },
  { title: '平均 LLM 耗时', dataIndex: 'avgLlmLatencyMs', width: 130 },
  { title: '工具调用', dataIndex: 'totalToolCalls', width: 100 },
  { title: '错误数', dataIndex: 'totalErrors', width: 90 },
  { title: '开始时间', dataIndex: 'startedAt', width: 170 },
]

const pagination = computed(() => ({
  current: pageData.value.pageNumber,
  pageSize: pageData.value.pageSize,
  total: pageData.value.totalRow,
  showSizeChanger: true,
  pageSizeOptions: ['10', '20', '50'],
  showTotal: (total: number) => `共 ${total} 条`,
}))

const fetchSessions = async () => {
  loading.value = true
  try {
    const res = await listMonitorSessions({
      pageNum: pageData.value.pageNumber,
      pageSize: pageData.value.pageSize,
      appId: query.appId || undefined,
      userId: query.userId || undefined,
      status: query.status || undefined,
    })
    if (res.data.code === 0 && res.data.data) {
      pageData.value = res.data.data
      return
    }
    message.error(`获取运行数据失败：${res.data.message}`)
  } catch {
    message.error('获取运行数据失败')
  } finally {
    loading.value = false
  }
}

const doSearch = () => {
  pageData.value.pageNumber = 1
  fetchSessions()
}

const resetSearch = () => {
  query.appId = ''
  query.userId = ''
  query.status = undefined
  pageData.value.pageNumber = 1
  fetchSessions()
}

const onTableChange = (page: { current?: number; pageSize?: number }) => {
  pageData.value.pageNumber = page.current || 1
  pageData.value.pageSize = page.pageSize || pageData.value.pageSize
  fetchSessions()
}

const statusLabel = (status?: string) =>
  ({ running: '运行中', completed: '已完成', stopped: '已停止', failed: '失败' } as Record<string, string>)[
    status || ''
  ] ||
  status ||
  '-'

const statusColor = (status?: string) =>
  ({ running: 'blue', completed: 'green', stopped: 'orange', failed: 'red' } as Record<string, string>)[
    status || ''
  ] || 'default'

// 后端耗时为毫秒，页面统一按秒展示
const formatDuration = (value?: number) =>
  value == null ? '-' : `${(Number(value) / 1000).toFixed(2)}s`

const formatDateTime = (value?: string) => (value ? dayjs(value).format('YYYY-MM-DD HH:mm:ss') : '-')

onMounted(() => {
  fetchSessions()
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

.search-form {
  row-gap: 8px;
}

.subtle-text {
  color: rgba(0, 0, 0, 0.45);
  font-size: 12px;
}
</style>
