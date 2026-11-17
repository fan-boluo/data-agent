<template>
  <div v-if="chartOption || images?.length || report" class="tool-result-media">
    <!-- CHART：ECharts option 直接渲染，可交互（tooltip/缩放/图例切换），无需落盘图片 -->
    <div v-if="chartOption" class="media-chart">
      <div ref="chartRef" class="media-chart-canvas"></div>
    </div>

    <!-- IMAGE：普通图片，output-file 接口 blob 拉取后渲染（img 不能带认证头） -->
    <div v-if="imageItems.length" class="media-images">
      <a-image-preview-group>
        <a-image
          v-for="img in imageItems"
          :key="img.path"
          :src="img.url"
          :alt="img.path"
          class="media-image"
        />
      </a-image-preview-group>
    </div>
    <div v-if="failedPaths.length" class="media-image-failed">
      图表加载失败（{{ failedPaths.length }} 张）
    </div>

    <!-- 报告入口：弹窗渲染 markdown + blob 下载 -->
    <div v-if="report" class="media-report">
      <span class="media-report-icon">📄</span>
      <span class="media-report-title">{{ report.title || '分析报告' }}</span>
      <a-button size="small" type="link" @click="reportVisible = true">查看报告</a-button>
      <a-button size="small" type="link" :loading="downloading" @click="downloadReport">下载</a-button>
    </div>

    <a-modal
      v-model:open="reportVisible"
      :title="report?.title || '分析报告'"
      width="720px"
      :footer="null"
    >
      <div class="report-modal-body">
        <MarkdownRenderer :content="report?.markdown || ''" />
      </div>
      <div class="report-modal-foot">
        <a-button size="small" :loading="downloading" @click="downloadReport">下载报告 (.md)</a-button>
      </div>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import * as echarts from 'echarts'
import MarkdownRenderer from '@/components/MarkdownRenderer.vue'
import request from '@/request'

// 工具产物报告元数据（后端 extras.report，file_name/created_at 与 fileName/createdAt 兼容）
interface ToolReportMeta {
  title?: string
  path?: string
  fileName?: string
  markdown?: string
  createdAt?: number
}

const props = defineProps<{
  appId?: string
  images?: string[]
  report?: ToolReportMeta | null
  // ECharts 配置（后端 extras.chart_option，message_type=CHART）
  chartOption?: Record<string, any> | null
}>()

const imageItems = ref<{ path: string; url: string }[]>([])
const failedPaths = ref<string[]>([])
const createdUrls: string[] = []
const downloading = ref(false)
const reportVisible = ref(false)

// 认证头取法与页面 getAuthHeaders 一致（localStorage token）
const getAuthHeaders = (): Record<string, string> => {
  const token = localStorage.getItem('token')
  if (!token) return {}
  return { Authorization: `Bearer ${token}` }
}

// 拉取单张图片为 objectURL（JWT 认证，<img> 无法直接带 header）
const loadImage = async (path: string) => {
  if (!props.appId || !path || imageItems.value.some(i => i.path === path)) return
  const baseURL = request.defaults.baseURL || ''
  try {
    const res = await fetch(`${baseURL}/api/ai/output-file?appId=${encodeURIComponent(props.appId)}&path=${encodeURIComponent(path)}`, {
      headers: { ...getAuthHeaders() },
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    createdUrls.push(url)
    imageItems.value.push({ path, url })
  } catch (e) {
    console.error('加载图表失败:', path, e)
    failedPaths.value.push(path)
  }
}

// images 变化时增量加载新图
watch(
  () => props.images,
  (imgs) => { for (const p of imgs || []) loadImage(p) },
  { immediate: true, deep: true },
)

// 报告下载：blob 拉取 reports/<fileName> 后触发浏览器保存
const downloadReport = async () => {
  const rep = props.report
  const fileName = rep?.fileName || rep?.title || 'report.md'
  if (!props.appId || !rep) return
  downloading.value = true
  try {
    const baseURL = request.defaults.baseURL || ''
    const res = await fetch(`${baseURL}/api/ai/output-file?appId=${encodeURIComponent(props.appId)}&path=${encodeURIComponent('reports/' + fileName)}`, {
      headers: { ...getAuthHeaders() },
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = fileName
    a.click()
    URL.revokeObjectURL(url)
  } catch (e: any) {
    console.error('下载报告失败:', e)
  } finally {
    downloading.value = false
  }
}

// ---- ECharts 渲染（与 AnalysisStepCard 同一套 init/dispose 写法） ----
const chartRef = ref<HTMLElement | null>(null)
let chartInstance: echarts.ECharts | null = null
let chartObserver: ResizeObserver | null = null

const renderChart = async () => {
  if (!props.chartOption) return
  await nextTick()
  if (!chartRef.value) return
  if (!chartInstance) {
    chartInstance = echarts.init(chartRef.value)
    // 容器宽度变化（窗口缩放/面板展开）时自适应
    chartObserver = new ResizeObserver(() => chartInstance?.resize())
    chartObserver.observe(chartRef.value)
  }
  chartInstance.setOption(props.chartOption, true)
  chartInstance.resize()
}

// chartOption 就绪/更新时重绘
watch(() => props.chartOption, () => renderChart(), { immediate: true, deep: true })

// 释放 objectURL
onBeforeUnmount(() => {
  for (const url of createdUrls) URL.revokeObjectURL(url)
  chartObserver?.disconnect()
  chartObserver = null
  chartInstance?.dispose()
  chartInstance = null
})
</script>

<style scoped>
.tool-result-media {
  margin: 6px 0;
}

.media-chart {
  width: 100%;
  max-width: 560px;
  border: 1px solid var(--border-light, #e5e7eb);
  border-radius: 6px;
  padding: 4px;
  background: #fff;
}

.media-chart-canvas {
  width: 100%;
  height: 260px;
}

.media-images {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.media-image :deep(img) {
  max-width: 240px;
  max-height: 180px;
  border-radius: 6px;
  border: 1px solid var(--border-light, #e5e7eb);
}

.media-image-failed {
  font-size: 12px;
  color: #ff4d4f;
}

.media-report {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  margin-top: 6px;
  background: #f6ffed;
  border: 1px solid #b7eb8f;
  border-radius: 6px;
}

.media-report-icon { font-size: 16px; }
.media-report-title {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  font-weight: 500;
  color: #389e0d;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.report-modal-body {
  max-height: 60vh;
  overflow-y: auto;
}

.report-modal-foot {
  margin-top: 12px;
  text-align: right;
}
</style>
