import { ref } from 'vue'
import request from '@/request'
import { API_BASE_URL } from '@/config/env'
import { getAuthHeaders } from './chatHelpers'

/**
 * 输入区状态：输入文本 + 可选数据集。
 * 数据集来自后端 /api/ai/datasets；为空时不展示选择器（agent 分不清时会反问）。
 */
export function useChatInput() {
  const userInput = ref('')
  const datasets = ref<Array<{ value: string; label: string }>>([])
  const currentDb = ref<string | undefined>(undefined)

  const fetchDatasets = async () => {
    try {
      const baseURL = request.defaults.baseURL || API_BASE_URL
      const res = await fetch(`${baseURL}/api/ai/datasets`, { headers: { ...getAuthHeaders() } })
      if (!res.ok) return
      const json = await res.json()
      if (Array.isArray(json?.data)) datasets.value = json.data
    } catch (error) {
      console.error('获取数据集列表失败：', error)
    }
  }

  return { userInput, datasets, currentDb, fetchDatasets }
}
