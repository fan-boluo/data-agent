<template>
  <div class="daily-stats-panel">
    <a-card class="panel-card" title="执行统计">
      <a-form layout="inline" class="filter-form">
        <a-form-item label="日期">
          <a-select
            v-model:value="dateFilter"
            :options="dateOptions"
            allow-clear
            placeholder="全部日期"
            style="width: 160px"
          />
        </a-form-item>
        <a-form-item :label="dimensionLabel">
          <a-select
            v-model:value="nameFilter"
            :options="nameOptions"
            allow-clear
            show-search
            option-filter-prop="label"
            :placeholder="`全部${dimensionLabel}`"
            style="width: 200px"
          />
        </a-form-item>
        <a-form-item>
          <a-space>
            <a-button @click="resetFilters">重置</a-button>
            <a-button :loading="loading" @click="loadStats">刷新</a-button>
          </a-space>
        </a-form-item>
      </a-form>
      <div class="filter-hint">仅展示近 7 天</div>
      <a-divider />
      <a-table
        row-key="rowKey"
        :loading="loading"
        :columns="columns"
        :data-source="tableRows"
        :pagination="pagination"
        :scroll="{ x: 760 }"
      />
    </a-card>

    <a-card class="panel-card" title="每日趋势">
      <template #extra>
        <a-segmented v-model:value="metric" :options="metricOptions" size="small" />
      </template>
      <a-spin :spinning="loading">
        <div v-show="hasData" ref="chartRef" class="chart-container" />
        <a-empty v-if="!loading && !hasData" description="近 7 天暂无执行数据" />
      </a-spin>
    </a-card>
  </div>
</template>

<script lang="ts" setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import * as echarts from 'echarts'

// 通用行：name 为工具名或模型名，由使用方归一化后传入
interface DailyStatRow {
  name: string
  date: string
  total: number
  success: number
  failure: number
}

interface DailyStatsPayload {
  dates: string[]
  rows: DailyStatRow[]
}

const props = defineProps<{
  dimensionLabel: string
  fetcher: () => Promise<DailyStatsPayload>
}>()

// 指标切换：折线图展示执行/成功/失败次数
const metric = ref<'total' | 'success' | 'failure'>('total')
const metricOptions = [
  { label: '执行次数', value: 'total' },
  { label: '成功次数', value: 'success' },
  { label: '失败次数', value: 'failure' },
]

// 折线配色（与全局科技蓝色系一致）
const SERIES_COLORS = [
  '#3B82F6',
  '#6366F1',
  '#10B981',
  '#F59E0B',
  '#EF4444',
  '#8B5CF6',
  '#14B8A6',
  '#F97316',
]

const loading = ref(false)
const dates = ref<string[]>([])
const rows = ref<DailyStatRow[]>([])
// 表格筛选（仅作用于表格，折线图始终展示完整 7 天）
const dateFilter = ref<string>()
const nameFilter = ref<string>()
const chartRef = ref<HTMLDivElement>()
let chartInstance: echarts.ECharts | null = null

const columns = [
  { title: '日期', dataIndex: 'date', width: 140 },
  { title: props.dimensionLabel, dataIndex: 'name', width: 220 },
  {
    title: '执行总次数',
    dataIndex: 'total',
    width: 120,
    sorter: (a: DailyStatRow, b: DailyStatRow) => a.total - b.total,
  },
  {
    title: '成功次数',
    dataIndex: 'success',
    width: 120,
    sorter: (a: DailyStatRow, b: DailyStatRow) => a.success - b.success,
  },
  {
    title: '失败次数',
    dataIndex: 'failure',
    width: 120,
    sorter: (a: DailyStatRow, b: DailyStatRow) => a.failure - b.failure,
  },
]

const pagination = {
  pageSize: 10,
  showSizeChanger: true,
  pageSizeOptions: ['10', '20', '50'],
  showTotal: (total: number) => `共 ${total} 条`,
}

// 表格行：先按日期/名称筛选，再补上稳定 rowKey（默认保持后端日期倒序）
const tableRows = computed(() =>
  rows.value
    .filter((item) => !dateFilter.value || item.date === dateFilter.value)
    .filter((item) => !nameFilter.value || item.name === nameFilter.value)
    .map((item) => ({
      ...item,
      rowKey: `${item.name}-${item.date}`,
    })),
)

// 筛选项：日期取近 7 天（新→旧），名称取出现过的工具/模型
const dateOptions = computed(() =>
  [...dates.value].reverse().map((date) => ({ label: date, value: date })),
)
const nameOptions = computed(() =>
  [...new Set(rows.value.map((item) => item.name))].map((name) => ({ label: name, value: name })),
)

const resetFilters = () => {
  dateFilter.value = undefined
  nameFilter.value = undefined
}

const hasData = computed(() => rows.value.length > 0)

// 名称列表（折线图每条线代表一个工具/模型）
const names = computed(() => [...new Set(rows.value.map((item) => item.name))])

// 名称 × 日期 → 指标值；缺失日期补 0 保证折线连续
const buildSeries = () => {
  const index = new Map(rows.value.map((item) => [`${item.name}-${item.date}`, item]))
  return names.value.map((name, i) => ({
    name,
    type: 'line',
    smooth: true,
    symbolSize: 6,
    data: dates.value.map((date) => index.get(`${name}-${date}`)?.[metric.value] ?? 0),
    itemStyle: { color: SERIES_COLORS[i % SERIES_COLORS.length] },
    lineStyle: { color: SERIES_COLORS[i % SERIES_COLORS.length] },
  }))
}

const renderChart = async () => {
  if (!hasData.value || !chartRef.value) return
  await nextTick()
  if (!chartInstance) chartInstance = echarts.init(chartRef.value)
  chartInstance.setOption(
    {
      tooltip: { trigger: 'axis' },
      legend: { data: names.value, top: 0 },
      grid: { left: '3%', right: '4%', bottom: '3%', top: 48, containLabel: true },
      xAxis: { type: 'category', boundaryGap: false, data: dates.value },
      yAxis: { type: 'value', minInterval: 1, name: '次数' },
      series: buildSeries(),
    },
    true,
  )
}

const loadStats = async () => {
  loading.value = true
  try {
    const payload = await props.fetcher()
    dates.value = payload.dates || []
    rows.value = payload.rows || []
    loading.value = false
    await renderChart()
  } catch {
    message.error(`获取${props.dimensionLabel}统计数据失败`)
  } finally {
    loading.value = false
  }
}

// 切换指标后重绘折线
watch(metric, () => renderChart())

const handleResize = () => chartInstance?.resize()

onMounted(() => {
  loadStats()
  window.addEventListener('resize', handleResize)
})

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
  chartInstance?.dispose()
  chartInstance = null
})
</script>

<style scoped>
.panel-card {
  margin-bottom: 16px;
}

.filter-form {
  row-gap: 8px;
}

.filter-hint {
  color: rgba(0, 0, 0, 0.45);
  font-size: 12px;
}

.chart-container {
  width: 100%;
  height: 400px;
}
</style>
