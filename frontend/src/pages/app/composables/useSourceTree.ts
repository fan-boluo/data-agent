import { computed, ref, type Ref } from 'vue'
import { message, Modal } from 'ant-design-vue'
import request from '@/request'
import { API_BASE_URL } from '@/config/env'

export interface UseSourceTreeOptions {
  appId: Ref<string | undefined>
  /** 重命名后关闭旧路径页签（由编辑器页签域提供） */
  closeTabOnRename: (path: string) => void
  /** 删除文件/目录后关闭受影响页签（由编辑器页签域提供） */
  closeTabsForDeletedPath: (deletedPath: string, deletedIsDir: boolean) => void
}

/**
 * 源码目录树 / 数据表面板域：侧栏 Tab 切换、树的加载与乐观增删改、
 * 文件/文件夹操作（新建/重命名/上传/删除）的弹窗状态与请求。
 */
export function useSourceTree(options: UseSourceTreeOptions) {
  const { appId, closeTabOnRename, closeTabsForDeletedPath } = options

  const sidePanelTab = ref<'data' | 'files'>('files')
  const sidePanelVisible = ref(true)
  const sourceFileTree = ref<any[]>([])
  const sourceRawNodes = ref<any[]>([])
  const loadingSourceTree = ref(false)

  const selectedFileNode = ref<any>(null)
  const selectedNodeKey = ref<string>('')

  const dbTables = ref<any[]>([])
  const loadingDbTables = ref(false)

  const dbTableTree = computed(() => {
    return dbTables.value.map((t) => ({
      key: t.name,
      title: t.name,
      isLeaf: true,
    }))
  })

  const buildSourceTree = (nodes: any[]): any[] => {
    return nodes.map((node) => ({
      key: node.path,
      title: node.name,
      isLeaf: node.type === 'file',
      children: node.children ? buildSourceTree(node.children) : undefined,
      dataRef: node,
    }))
  }

  // Optimistic tree mutations — modify sourceRawNodes then rebuild tree from raw nodes
  const rebuildTreeFromRaw = () => {
    sourceFileTree.value = buildSourceTree(sourceRawNodes.value)
  }

  const addNodeToRaw = (parentPath: string, newNode: { path: string; name: string; type: string; children?: any[] }) => {
    if (!parentPath) {
      sourceRawNodes.value.push(newNode)
      rebuildTreeFromRaw()
      return
    }
    const walk = (nodes: any[]): boolean => {
      for (const n of nodes) {
        if (n.path === parentPath && n.type === 'dir') {
          if (!n.children) n.children = []
          n.children.push(newNode)
          return true
        }
        if (n.children && walk(n.children)) return true
      }
      return false
    }
    walk(sourceRawNodes.value)
    rebuildTreeFromRaw()
  }

  const renameNodeInRaw = (oldPath: string, newPath: string, newName: string) => {
    const walk = (nodes: any[]): boolean => {
      for (const n of nodes) {
        if (n.path === oldPath) {
          n.path = newPath
          n.name = newName
          return true
        }
        if (n.children && walk(n.children)) return true
      }
      return false
    }
    walk(sourceRawNodes.value)
    rebuildTreeFromRaw()
  }

  const loadSourceTree = async () => {
    if (!appId.value) return
    loadingSourceTree.value = true
    try {
      const baseURL = request.defaults.baseURL || API_BASE_URL
      const res = await request.get(`${baseURL}/api/app/code/tree/${appId.value}`)
      if (res.data.code === 0) {
        sourceRawNodes.value = res.data.data || []
        sourceFileTree.value = buildSourceTree(sourceRawNodes.value)
      } else {
        message.error('获取源码目录失败：' + res.data.message)
      }
    } catch (error) {
      console.error('获取源码目录失败:', error)
      message.error('获取源码目录失败')
    } finally {
      loadingSourceTree.value = false
    }
  }

  const loadDbTables = async () => {
    if (!appId.value) return
    loadingDbTables.value = true
    try {
      const baseURL = request.defaults.baseURL || API_BASE_URL
      const res = await request.get(`${baseURL}/api/app/db/tables/${appId.value}`)
      if (res.data.code === 0) {
        dbTables.value = res.data.data || []
      } else {
        dbTables.value = []
      }
    } catch (error) {
      console.error('获取数据表失败:', error)
      dbTables.value = []
    } finally {
      loadingDbTables.value = false
    }
  }

  const switchSidePanel = (tab: 'data' | 'files') => {
    if (sidePanelTab.value === tab) {
      sidePanelVisible.value = !sidePanelVisible.value
      return
    }
    sidePanelTab.value = tab
    sidePanelVisible.value = true
    if (tab === 'files' && !sourceFileTree.value.length) {
      loadSourceTree()
    } else if (tab === 'data' && !dbTables.value.length) {
      loadDbTables()
    }
  }

  // 树节点选中（页面模板事件转发）
  const selectTreeNode = (node: any) => {
    selectedFileNode.value = node
    selectedNodeKey.value = node.key
  }

  // File operations
  const createFileModalVisible = ref(false)
  const createFolderModalVisible = ref(false)
  const newItemName = ref('')
  const newItemParentPath = ref('')
  const fileUploadInput = ref<HTMLInputElement>()
  const fileOpLoading = ref(false)

  const openCreateFileModal = () => {
    const node = selectedFileNode.value
    newItemParentPath.value = node ? (node.dataRef?.type === 'dir' ? node.key : node.key.substring(0, node.key.lastIndexOf('/') + 1) || '') : ''
    newItemName.value = ''
    createFileModalVisible.value = true
  }

  const openCreateFolderModal = () => {
    const node = selectedFileNode.value
    newItemParentPath.value = node ? (node.dataRef?.type === 'dir' ? node.key : node.key.substring(0, node.key.lastIndexOf('/') + 1) || '') : ''
    newItemName.value = ''
    createFolderModalVisible.value = true
  }

  const doCreateFile = async () => {
    if (!newItemName.value.trim()) return
    fileOpLoading.value = true
    try {
      const baseURL = request.defaults.baseURL || API_BASE_URL
      const filePath = newItemParentPath.value ? `${newItemParentPath.value}/${newItemName.value.trim()}` : newItemName.value.trim()
      const res = await request.post(`${baseURL}/api/app/code/file/create/${appId.value}`, null, {
        params: { path: filePath },
      })
      if (res.data.code === 0) {
        message.success('文件创建成功')
        createFileModalVisible.value = false
        addNodeToRaw(newItemParentPath.value, { path: filePath, name: newItemName.value.trim(), type: 'file' })
      } else {
        message.error('创建失败：' + res.data.message)
      }
    } catch (error: any) {
      message.error('创建失败：' + (error.message || '未知错误'))
    } finally {
      fileOpLoading.value = false
    }
  }

  const doCreateFolder = async () => {
    if (!newItemName.value.trim()) return
    fileOpLoading.value = true
    try {
      const baseURL = request.defaults.baseURL || API_BASE_URL
      const folderPath = newItemParentPath.value ? `${newItemParentPath.value}/${newItemName.value.trim()}` : newItemName.value.trim()
      const res = await request.post(`${baseURL}/api/app/code/folder/create/${appId.value}`, null, {
        params: { path: folderPath },
      })
      if (res.data.code === 0) {
        message.success('文件夹创建成功')
        createFolderModalVisible.value = false
        addNodeToRaw(newItemParentPath.value, { path: folderPath, name: newItemName.value.trim(), type: 'dir', children: [] })
      } else {
        message.error('创建失败：' + res.data.message)
      }
    } catch (error: any) {
      message.error('创建失败：' + (error.message || '未知错误'))
    } finally {
      fileOpLoading.value = false
    }
  }

  // Rename
  const renameModalVisible = ref(false)
  const renameNewName = ref('')

  const openRenameModal = () => {
    const node = selectedFileNode.value
    if (!node) return
    renameNewName.value = node.title
    renameModalVisible.value = true
  }

  const doRename = async () => {
    const node = selectedFileNode.value
    if (!node || !renameNewName.value.trim() || renameNewName.value.trim() === node.title) {
      renameModalVisible.value = false
      return
    }
    fileOpLoading.value = true
    try {
      const baseURL = request.defaults.baseURL || API_BASE_URL
      const parentPath = node.key.includes('/') ? node.key.substring(0, node.key.lastIndexOf('/') + 1) : ''
      const newPath = parentPath ? `${parentPath}${renameNewName.value.trim()}` : renameNewName.value.trim()
      const res = await request.post(`${baseURL}/api/app/code/rename/${appId.value}`, null, {
        params: { from: node.key, to: newPath },
      })
      if (res.data.code === 0) {
        message.success('重命名成功')
        renameModalVisible.value = false
        renameNodeInRaw(node.key, newPath, renameNewName.value.trim())
        // Close old tab for this file
        closeTabOnRename(node.key)
        selectedFileNode.value = null
        await loadSourceTree()
      } else {
        message.error('重命名失败：' + res.data.message)
      }
    } catch (error: any) {
      message.error('重命名失败：' + (error.message || '未知错误'))
    } finally {
      fileOpLoading.value = false
    }
  }

  const triggerFileUpload = () => {
    if (!fileUploadInput.value) {
      const input = document.createElement('input')
      input.type = 'file'
      input.style.display = 'none'
      input.addEventListener('change', handleFileUpload)
      document.body.appendChild(input)
      fileUploadInput.value = input
    }
    fileUploadInput.value.click()
  }

  const handleFileUpload = async (e: Event) => {
    const target = e.target as HTMLInputElement
    const file = target.files?.[0]
    if (!file) return
    try {
      const baseURL = request.defaults.baseURL || API_BASE_URL
      const node = selectedFileNode.value
      const parentPath = node ? (node.dataRef?.type === 'dir' ? node.key : node.key.substring(0, node.key.lastIndexOf('/') + 1) || '') : ''
      const filePath = parentPath ? `${parentPath}/${file.name}` : file.name
      const formData = new FormData()
      formData.append('file', file)
      const res = await request.post(`${baseURL}/api/app/code/file/upload/${appId.value}`, formData, {
        params: { path: filePath },
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      if (res.data.code === 0) {
        message.success('上传成功')
        await loadSourceTree()
      } else {
        message.error('上传失败：' + res.data.message)
      }
    } catch (error: any) {
      message.error('上传失败：' + (error.message || '未知错误'))
    } finally {
      target.value = ''
    }
  }

  const deleteSelectedNode = () => {
    const node = selectedFileNode.value
    if (!node) return
    const name = node.title
    const isDir = node.dataRef?.type === 'dir'
    Modal.confirm({
      title: isDir ? '删除文件夹' : '删除文件',
      content: `确定要删除「${name}」吗？${isDir ? '将同时删除文件夹内的所有内容。' : ''}此操作不可撤销。`,
      okText: '删除',
      okType: 'danger',
      cancelText: '取消',
      onOk: async () => {
        try {
          const baseURL = request.defaults.baseURL || API_BASE_URL
          const res = await request.delete(`${baseURL}/api/app/code/${appId.value}`, {
            params: { path: node.key },
          })
          if (res.data.code === 0) {
            message.success('删除成功')

            // Close all tabs that are in/under the deleted path
            closeTabsForDeletedPath(node.key, node.dataRef?.type === 'dir')

            selectedFileNode.value = null
            await loadSourceTree()
          } else {
            message.error('删除失败：' + res.data.message)
          }
        } catch (error: any) {
          message.error('删除失败：' + (error.message || '未知错误'))
        }
      },
    })
  }

  return {
    sidePanelTab,
    sidePanelVisible,
    sourceFileTree,
    sourceRawNodes,
    loadingSourceTree,
    selectedFileNode,
    selectedNodeKey,
    dbTables,
    loadingDbTables,
    dbTableTree,
    loadSourceTree,
    loadDbTables,
    switchSidePanel,
    selectTreeNode,
    createFileModalVisible,
    createFolderModalVisible,
    newItemName,
    newItemParentPath,
    fileOpLoading,
    openCreateFileModal,
    openCreateFolderModal,
    doCreateFile,
    doCreateFolder,
    renameModalVisible,
    renameNewName,
    openRenameModal,
    doRename,
    triggerFileUpload,
    deleteSelectedNode,
  }
}
