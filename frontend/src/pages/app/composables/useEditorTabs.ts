import { computed, nextTick, ref, watch, type ComputedRef, type Ref } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { useOpenedChatsStore } from '@/stores/openedChats'
import request from '@/request'
import { API_BASE_URL } from '@/config/env'
import type { MonacoEditor } from '@guolao/vue-monaco-editor'

export interface FileTab {
  path: string
  name: string
  content: string
  originalContent: string
  isLoading: boolean
  lastAccessed: number
  isDirty: boolean
  language: string
  abortController?: AbortController | null
}

const MAX_OPEN_TABS = 5

const LANG_MAP: Record<string, string> = {
  js: 'javascript', ts: 'typescript', jsx: 'javascript', tsx: 'typescript',
  vue: 'html', html: 'html', css: 'css', scss: 'scss', less: 'less',
  json: 'json', py: 'python', md: 'markdown', yaml: 'yaml', yml: 'yaml',
  sh: 'bash', sql: 'sql', java: 'java', go: 'go', rs: 'rust',
  xml: 'xml', txt: 'plaintext', c: 'c', cpp: 'cpp', h: 'c',
}

const getLanguage = (fileName: string): string => {
  const ext = fileName.split('.').pop()?.toLowerCase() ?? ''
  return LANG_MAP[ext] || 'plaintext'
}

/**
 * 代码编辑器页签域：Monaco 编辑器、多页签开关与脏状态、文件内容读写保存。
 * 页签的删除/重命名联动由源码树域回调本域的 closeTabOnRename / closeTabsForDeletedPath。
 */
export function useEditorTabs(appId: Ref<string | undefined>, canOperateApp: Ref<boolean>) {
  const openedChatsStore = useOpenedChatsStore()

  const fileTabs = ref<FileTab[]>([])
  const activeFileTab = ref<string>('')
  const activeTabData = computed(() => {
    return fileTabs.value.find(t => t.path === activeFileTab.value)
  })

  // Monaco editor state
  const editorTheme = ref<'vs' | 'vs-dark'>('vs')
  const monacoEditor = ref<MonacoEditor | null>(null)
  const editorMounted = ref(false)

  const showLoading: ComputedRef<boolean> = computed(() => {
    const tab = activeTabData.value
    const shouldShow = !!tab?.isLoading
    if (tab) {
      console.log(`[ShowLoading] Tab: ${tab.path}, isLoading: ${tab.isLoading}, shouldShow: ${shouldShow}`)
    }
    return shouldShow
  })

  const editorOptions = computed(() => ({
    readOnly: !canOperateApp.value,
    domReadOnly: !canOperateApp.value,
    minimap: { enabled: true },
    fontSize: 13,
    fontFamily: "'SF Mono', 'Fira Code', Menlo, Consolas, monospace",
    lineNumbers: 'on' as const,
    renderWhitespace: 'selection' as const,
    tabSize: 2,
    automaticLayout: true,
    scrollBeyondLastLine: false,
    wordWrap: 'off' as const,
    bracketPairColorization: { enabled: true },
    padding: { top: 8 },
  }))

  const loadFileContent = async (filePath: string, signal?: AbortSignal): Promise<string> => {
    const baseURL = request.defaults.baseURL || API_BASE_URL
    const res = await request.get(`${baseURL}/api/app/code/file/${appId.value}`, { params: { path: filePath }, signal })
    if (res.data.code === 0) {
      const text = res.data.data ?? ''
      const lines = text.split('\n')
      const isPythonFile = filePath.endsWith('.py')
      return (!isPythonFile && lines.length > 100) ? lines.slice(0, 100).join('\n') : text
    }
    throw new Error(res.data.message || '读取文件失败')
  }

  const saveCurrentFile = async () => {
    const tab = activeTabData.value
    if (!tab || !tab.isDirty || !appId.value) return
    try {
      const baseURL = request.defaults.baseURL || API_BASE_URL
      const res = await request.post(
        `${baseURL}/api/app/code/file/${appId.value}`,
        { content: tab.content },
        { params: { path: tab.path } },
      )
      if (res.data.code === 0) {
        tab.originalContent = tab.content
        tab.isDirty = false
        message.success('已保存')
      } else {
        message.error('保存失败：' + res.data.message)
      }
    } catch (error: any) {
      console.error('保存文件失败:', error)
      message.error('保存失败：' + (error.message || '未知错误'))
    }
  }

  const handleEditorMount = (editor: MonacoEditor) => {
    console.log(`[EditorMount] Editor mounted for tab: ${activeFileTab.value}`)
    monacoEditor.value = editor
    editorMounted.value = true
    editor.addCommand(
      // Monaco KeyMod.CtrlCmd | Monaco KeyCode.KeyS
      2048 | 49,
      () => { saveCurrentFile() },
    )
  }

  const openFileInTab = async (filePath: string, fileName: string) => {
    const existing = fileTabs.value.find(t => t.path === filePath)
    if (existing) {
      existing.lastAccessed = Date.now()
      activeFileTab.value = filePath
      return
    }

    if (fileTabs.value.length >= MAX_OPEN_TABS) {
      let oldest = fileTabs.value[0]!
      for (let i = 1; i < fileTabs.value.length; i++) {
        const t = fileTabs.value[i]!
        if (t.lastAccessed < oldest.lastAccessed) {
          oldest = t
        }
      }
      if (oldest.isDirty) {
        message.warning('请先保存或关闭未保存的文件后再打开新文件')
        return
      }
      const idx = fileTabs.value.indexOf(oldest)
      fileTabs.value.splice(idx, 1)
    }

    const newTab: FileTab = {
      path: filePath,
      name: fileName,
      content: '',
      originalContent: '',
      isLoading: true,
      lastAccessed: Date.now(),
      isDirty: false,
      language: getLanguage(fileName),
      abortController: null,
    }
    fileTabs.value.push(newTab)
    activeFileTab.value = filePath

    try {
      const controller = new AbortController()
      newTab.abortController = controller
      console.log(`[FileLoad] Starting load: ${filePath}`)
      const text = await loadFileContent(filePath, controller.signal)
      console.log(`[FileLoad] Success: ${filePath}, content length: ${text.length}`)
      newTab.content = text
      newTab.originalContent = text
    } catch (error: any) {
      console.error(`[FileLoad] Error loading ${filePath}:`, error)
      const errMsg = error?.message || '未知错误'
      newTab.content = `// 加载失败: ${errMsg}\n// 请尝试重新打开此文件`
      newTab.originalContent = newTab.content
      message.error('读取文件失败：' + errMsg)
    } finally {
      newTab.isLoading = false
      if (newTab.abortController) newTab.abortController = null
      console.log(`[FileLoad] Finished: ${filePath}, isLoading now false`)
      // Force Vue to detect object property change by replacing the entire object
      const idx = fileTabs.value.findIndex(t => t.path === filePath)
      if (idx >= 0) {
        const cur = fileTabs.value[idx]
        if (cur) fileTabs.value[idx] = { ...cur }
        console.log(`[FileLoad] Replaced object at index ${idx}`)
      }
      await nextTick()
      console.log(`[FileLoad] After nextTick, activeTabData.isLoading: ${activeTabData.value?.isLoading}`)
    }
  }

  const activateFileTab = (path: string) => {
    const tab = fileTabs.value.find(t => t.path === path)
    if (tab) {
      console.log(`[TabSwitch] Switching to tab: ${path}`)
      tab.lastAccessed = Date.now()
      activeFileTab.value = path
      console.log(`[TabSwitch] activeFileTab is now: ${activeFileTab.value}`)
    }
  }

  const doCloseTab = (idx: number, path: string) => {
    fileTabs.value.splice(idx, 1)

    if (activeFileTab.value === path) {
      if (fileTabs.value.length > 0) {
        let mostRecent = fileTabs.value[0]!
        for (const t of fileTabs.value) {
          if (t.lastAccessed > mostRecent.lastAccessed) {
            mostRecent = t
          }
        }
        activeFileTab.value = mostRecent.path
      } else {
        activeFileTab.value = ''
      }
    }
  }

  const closeFileTab = (path: string) => {
    const idx = fileTabs.value.findIndex(t => t.path === path)
    if (idx === -1) return

    const tab = fileTabs.value[idx]
    if (!tab) return
    // Abort in-flight load if any
    if (tab.abortController) {
      try { tab.abortController.abort() } catch (e) { console.warn('abort failed', e) }
      tab.abortController = null
    }
    if (tab.isDirty) {
      Modal.confirm({
        title: '未保存的更改',
        content: `文件 "${tab.name}" 有未保存的更改，确定要关闭吗？`,
        okText: '关闭',
        cancelText: '取消',
        okType: 'danger',
        onOk: () => { doCloseTab(idx, path) },
      })
      return
    }
    doCloseTab(idx, path)
  }

  // 重命名后关闭旧路径页签（沿用原逻辑：激活相邻页签兜底）
  const closeTabOnRename = (path: string) => {
    const oldTabIdx = fileTabs.value.findIndex(t => t.path === path)
    if (oldTabIdx >= 0) {
      fileTabs.value.splice(oldTabIdx, 1)
      if (activeFileTab.value === path) {
        activeFileTab.value = fileTabs.value.length > 0 ? fileTabs.value[Math.min(oldTabIdx, fileTabs.value.length - 1)]!.path : ''
      }
    }
  }

  // 删除文件/目录后关闭受影响页签（含中止加载中的请求）
  const closeTabsForDeletedPath = (deletedPath: string, deletedIsDir: boolean) => {
    // Abort any in-flight loads for affected tabs
    for (const t of fileTabs.value) {
      if (t.path === deletedPath || (deletedIsDir && t.path.startsWith(deletedPath + '/'))) {
        if (t.abortController) {
          try { t.abortController.abort() } catch (e) { /* ignore */ }
          t.abortController = null
        }
      }
    }
    fileTabs.value = fileTabs.value.filter(t => {
      if (t.path === deletedPath) return false
      if (deletedIsDir && t.path.startsWith(deletedPath + '/')) return false
      return true
    })
    if (activeFileTab.value && !fileTabs.value.find(t => t.path === activeFileTab.value)) {
      activeFileTab.value = fileTabs.value.length > 0 ? fileTabs.value[fileTabs.value.length - 1]!.path : ''
    }
  }

  // Reset editor mounted flag when switching tabs (key changes, Monaco recreated)
  watch(activeFileTab, () => {
    console.log(`[EditorLifecycle] activeFileTab changed to: ${activeFileTab.value}`)
    editorMounted.value = false
    monacoEditor.value = null
  })

  // Watch for content changes to track dirty state
  watch(
    () => fileTabs.value.map(t => ({ path: t.path, content: t.content })),
    () => {
      for (const tab of fileTabs.value) {
        if (tab.originalContent !== undefined) {
          tab.isDirty = tab.content !== tab.originalContent
        }
      }
    },
    { deep: true },
  )

  // 有无未保存文件更改同步到页签栏（关闭页签时的确认提示依据）
  watch(
    () => fileTabs.value.some((t) => t.isDirty),
    (dirty) => {
      if (appId.value) openedChatsStore.setDirty(appId.value, dirty)
    },
  )

  return {
    fileTabs,
    activeFileTab,
    activeTabData,
    editorTheme,
    monacoEditor,
    editorMounted,
    showLoading,
    editorOptions,
    saveCurrentFile,
    handleEditorMount,
    openFileInTab,
    activateFileTab,
    closeFileTab,
    closeTabOnRename,
    closeTabsForDeletedPath,
  }
}
