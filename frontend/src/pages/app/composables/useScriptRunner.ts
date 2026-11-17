import { ref, type ComputedRef, type Ref } from 'vue'
import { message } from 'ant-design-vue'
import request from '@/request'
import { API_BASE_URL } from '@/config/env'
import type { FileTab } from './useEditorTabs'
import { getAuthHeaders } from './chatHelpers'

export interface OutputLine {
  type: 'stdout' | 'stderr' | 'system'
  text: string
  timestamp: number
}

/**
 * 脚本运行与输出面板：Python 环境选择、SSE 流式运行输出、输出行增删。
 * 聊天流的 Log_Chunk / Terminal_Output 事件也经 appendOutput 写入输出面板。
 */
export function useScriptRunner(appId: Ref<string | undefined>, activeTabData: ComputedRef<FileTab | undefined>) {
  // Python env & script running
  const selectedPythonEnv = ref('model')
  const pythonEnvOptions = [
    { value: 'model', label: 'model' },
  ]
  const runningScript = ref(false)

  const outputLines = ref<OutputLine[]>([])
  const outputContainer = ref<HTMLElement>()

  const clearOutput = () => {
    outputLines.value = []
  }

  const appendOutput = (type: OutputLine['type'], text: string) => {
    outputLines.value.push({ type, text, timestamp: Date.now() })
    if (outputContainer.value) {
      outputContainer.value.scrollTop = outputContainer.value.scrollHeight
    }
  }

  const runScript = async () => {
    const tab = activeTabData.value
    if (!tab || !appId.value) return
    if (tab.isDirty) {
      message.warning('请先保存文件再运行')
      return
    }
    runningScript.value = true
    outputLines.value = []
    appendOutput('system', `>>> 运行: python ${tab.name}`)
    try {
      const baseURL = request.defaults.baseURL || API_BASE_URL
      const url = `${baseURL}/api/app/code/run/${appId.value}`
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
        body: JSON.stringify({
          path: tab.path,
          env: selectedPythonEnv.value,
        }),
      })
      if (!response.ok) {
        appendOutput('stderr', `运行失败: HTTP ${response.status}`)
        return
      }
      const reader = response.body?.getReader()
      if (!reader) {
        appendOutput('stderr', '运行失败: 无响应')
        return
      }
      const decoder = new TextDecoder()
      let buffer = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''
        for (const line of lines) {
          const trimmed = line.trim()
          if (!trimmed) continue
          // Strip SSE "data: " prefix
          if (!trimmed.startsWith('data: ')) continue
          const jsonStr = trimmed.slice(6)
          try {
            const event = JSON.parse(jsonStr)
            if (event.event_type === 'output') {
              const text = typeof event.data === 'string' ? event.data : event.data?.text || ''
              appendOutput(event.data?.stream || 'stdout', text)
            } else if (event.event_type === 'done') {
              appendOutput('system', `>>> 脚本结束，退出码: ${event.data?.code ?? 0}`)
            } else if (event.event_type === 'error') {
              appendOutput('stderr', event.data?.message || event.data || '未知错误')
            }
          } catch {
            appendOutput('stdout', trimmed)
          }
        }
      }
      if (buffer.trim() && buffer.trim().startsWith('data: ')) {
        const jsonStr = buffer.trim().slice(6)
        try {
          const event = JSON.parse(jsonStr)
          if (event.event_type === 'done') {
            appendOutput('system', `>>> 脚本结束，退出码: ${event.data?.code ?? 0}`)
          }
        } catch {
          appendOutput('stdout', buffer.trim())
        }
      }
    } catch (error: any) {
      appendOutput('stderr', `运行失败: ${error.message || '未知错误'}`)
    } finally {
      runningScript.value = false
    }
  }

  return { selectedPythonEnv, pythonEnvOptions, runningScript, outputLines, outputContainer, clearOutput, appendOutput, runScript }
}
