<template>
  <div id="appChatPage" class="ide-layout">
    <!-- Activity Bar (VS Code style) -->
    <div v-if="appId" class="activity-bar">
      <div class="activity-bar-top">
        <div
          class="activity-icon"
          :class="{ active: sidePanelTab === 'files' }"
          title="项目文件"
          @click="switchSidePanel('files')"
        >
          <FolderOutlined />
        </div>
        <div
          class="activity-icon"
          :class="{ active: sidePanelTab === 'data' }"
          title="数据表"
          @click="switchSidePanel('data')"
        >
          <DatabaseOutlined />
        </div>
      </div>
      <div class="activity-bar-bottom">
      </div>
    </div>

    <!-- Side Panel -->
    <div
      v-if="appId && sidePanelVisible"
      class="side-panel"
      :style="{ width: sidePanelWidth + 'px' }"
    >
      <div class="side-panel-header">
        <span class="side-panel-title">
          <FolderOutlined v-if="sidePanelTab === 'files'" />
          <DatabaseOutlined v-else />
          <span>{{ sidePanelTab === 'files' ? '项目文件' : '数据表' }}</span>
        </span>
        <!-- File operation buttons -->
        <span v-if="sidePanelTab === 'files' && canOperateApp" class="side-panel-actions">
          <a-tooltip title="新建文件">
            <a-button type="text" size="small" @click="openCreateFileModal">
              <template #icon><FileAddOutlined /></template>
            </a-button>
          </a-tooltip>
          <a-tooltip title="新建文件夹">
            <a-button type="text" size="small" @click="openCreateFolderModal">
              <template #icon><FolderAddOutlined /></template>
            </a-button>
          </a-tooltip>
          <a-tooltip title="上传文件">
            <a-button type="text" size="small" @click="triggerFileUpload">
              <template #icon><UploadOutlined /></template>
            </a-button>
          </a-tooltip>
          <a-tooltip title="下载文件">
            <a-button type="text" size="small" :disabled="!isFileNodeSelected" @click="downloadSelectedFile">
              <template #icon><DownloadOutlined /></template>
            </a-button>
          </a-tooltip>
          <a-tooltip title="重命名">
            <a-button type="text" size="small" :disabled="!selectedFileNode" @click="openRenameModal">
              <template #icon><EditOutlined /></template>
            </a-button>
          </a-tooltip>
          <a-tooltip title="删除选中项">
            <a-button type="text" size="small" :disabled="!selectedFileNode" @click="deleteSelectedNode">
              <template #icon><DeleteOutlined /></template>
            </a-button>
          </a-tooltip>
        </span>
      </div>

      <div class="side-panel-content">
        <template v-if="sidePanelTab === 'data'">
          <div v-if="loadingDbTables" class="sidebar-loading">
            <a-spin size="small" />
          </div>
          <div v-else-if="!dbTables.length" class="sidebar-empty">
            <p>{{ appInfo?.dbName ? '暂无数据表' : '未配置项目库' }}</p>
          </div>
          <a-tree
            v-else
            :tree-data="dbTableTree"
            :default-expand-all="true"
            class="sidebar-tree"
          />
        </template>
        <template v-else>
          <div v-if="loadingSourceTree" class="sidebar-loading">
            <a-spin size="small" />
          </div>
          <div v-else-if="!sourceFileTree.length" class="sidebar-empty">
            <p>暂无项目文件</p>
          </div>
          <a-tree
            v-else
            :tree-data="sourceFileTree"
            :selected-keys="selectedNodeKey ? [selectedNodeKey] : []"
            :default-expand-all="true"
            @select="handleTreeNodeSelect"
            @dblclick="handleSourceFileDoubleClick"
            class="sidebar-tree"
          />
        </template>
      </div>
    </div>

    <!-- Resizer: side panel ↔ center -->
    <div
      v-if="appId && sidePanelVisible"
      class="resizer"
      @mousedown="onResizeStart('sidebar', $event)"
    />

    <!-- Center Column: editor + output (vertical stack) -->
    <div class="center-column">
      <!-- Editor Panel -->
      <div class="center-work-area">
        <div class="file-tab-bar">
          <div class="file-tab-bar-left">
            <div
              v-for="tab in fileTabs"
              :key="tab.path"
              class="file-tab"
              :class="{ active: tab.path === activeFileTab }"
              @click="activateFileTab(tab.path)"
            >
              <span class="file-tab-dirty" v-if="tab.isDirty" title="未保存的更改"></span>
              <span class="file-tab-name">{{ tab.name }}</span>
              <span class="file-tab-close" @click.stop="closeFileTab(tab.path)">
                <CloseOutlined />
              </span>
            </div>
          </div>
          <div class="file-tab-bar-right">
            运行环境：
            <a-select
              v-model:value="selectedPythonEnv"
              size="small"
              style="width: 100px;"
              :options="pythonEnvOptions"
              placeholder="环境"
            />
            <a-tooltip title="运行脚本">
              <a-button
                type="text"
                size="small"
                :loading="runningScript"
                :disabled="!activeTabData || !activeTabData.name.endsWith('.py')"
                @click="runScript"
              >
                <template #icon><CaretRightOutlined /></template>
              </a-button>
            </a-tooltip>
          </div>
        </div>

        <div class="editor-area">
          <div v-if="!fileTabs.length" class="center-empty">
            <div class="center-empty-icon">
              <FileTextOutlined />
            </div>
            <p class="center-empty-title">暂无打开的文件</p>
            <p class="center-empty-hint">从左侧「项目文件」中点击文件查看源码</p>
          </div>

          <template v-else>
            <div class="editor-wrapper">
              <div v-if="showLoading" class="editor-loading-overlay">
                <a-spin size="default" />
                <span>加载中...</span>
              </div>

              <VueMonacoEditor
                v-if="activeTabData"
                :key="activeFileTab"
                v-model:value="activeTabData.content"
                :language="activeTabData.language"
                :theme="editorTheme"
                :options="editorOptions"
                class="monaco-editor-container"
                @mount="handleEditorMount"
              />
            </div>
          </template>
        </div>
      </div>

      <!-- Resizer: editor ↔ output -->
      <div
        v-if="fileTabs.length"
        class="resizer-horizontal"
        @mousedown="onResizeStart('output', $event)"
      />

      <!-- Output Panel -->
      <div v-if="fileTabs.length" class="output-panel" :style="{ height: outputHeight + 'px' }">
        <div class="output-header">
          <span class="output-title">输出</span>
          <a-button type="text" size="small" @click="clearOutput">
            <template #icon><ClearOutlined /></template>
          </a-button>
        </div>
        <div class="output-content" ref="outputContainer">
          <div v-if="!outputLines.length" class="output-empty">运行输出将显示在这里</div>
          <div
            v-for="(line, i) in outputLines"
            :key="i"
            class="output-line"
            :class="'output-' + line.type"
          >{{ line.text }}</div>
        </div>
      </div>
    </div>

    <!-- Resizer: center ↔ right chat -->
    <div
      class="resizer"
      @mousedown="onResizeStart('chat', $event)"
    />

    <!-- Right Chat Panel -->
    <div class="right-chat-panel" :style="{ width: chatWidth + 'px' }">
      <!-- Chat Header：标题即 Tab 切换（AI助手=对话 / 分析链路） -->
      <div class="chat-header">
        <div class="chat-header-tabs">
          <span
            class="chat-header-tab"
            :class="{ active: chatTab === 'chat' }"
            @click="chatTab = 'chat'"
          >AI助手</span>
          <span class="chat-header-tab-divider">|</span>
          <span
            class="chat-header-tab"
            :class="{ active: chatTab === 'trace' }"
            @click="chatTab = 'trace'"
          >分析链路</span>
        </div>
        <div class="chat-header-actions">
          <a-tooltip title="新建会话">
            <a-button type="text" size="small" @click="createNewSession">
              <template #icon><PlusOutlined /></template>
            </a-button>
          </a-tooltip>
          <a-tooltip title="会话历史">
            <a-button type="text" size="small" @click="toggleSessionHistory">
              <template #icon><HistoryOutlined /></template>
            </a-button>
          </a-tooltip>
        </div>
      </div>
      <!-- Session History Dropdown -->
      <ChatSessionDropdown
        v-if="sessionHistoryVisible"
        :session-list="sessionList"
        :loading="loadingSessionHistory"
        @load-session="loadSession"
      />

      <!-- 对话内容区 -->
      <div v-show="chatTab === 'chat'" class="chat-content">
        <div class="messages-container" ref="messagesContainer">
          <ChatMessageList :messages="messages" :app-id="appId" @jump-trace="jumpToTraceStep" />
        </div>

        <a-alert
          v-if="selectedElementInfo"
          class="selected-element-alert"
          type="info"
          closable
          @close="clearSelectedElement"
        >
          <template #message>
            <div class="selected-element-info">
              <div class="element-header">
                <span class="element-tag">{{ selectedElementInfo.tagName.toLowerCase() }}</span>
                <span v-if="selectedElementInfo.id" class="element-id">#{{ selectedElementInfo.id }}</span>
                <span v-if="selectedElementInfo.className" class="element-class">.{{ selectedElementInfo.className.split(' ').join('.') }}</span>
              </div>
              <div class="element-details">
                <div v-if="selectedElementInfo.textContent" class="element-item">
                  内容: {{ selectedElementInfo.textContent.substring(0, 50) }}{{ selectedElementInfo.textContent.length > 50 ? '...' : '' }}
                </div>
                <div v-if="selectedElementInfo.pagePath" class="element-item">页面路径: {{ selectedElementInfo.pagePath }}</div>
                <div class="element-item">选择器: <code class="element-selector-code">{{ selectedElementInfo.selector }}</code></div>
              </div>
            </div>
          </template>
        </a-alert>
      </div>

      <!-- 分析链路内容区：按 request/turn 组织的时间线 -->
      <div v-show="chatTab === 'trace'" class="trace-container">
        <div v-if="!activeTraceTurns.length" class="trace-empty">
          <div class="trace-empty-icon">🔍</div>
          <p class="trace-empty-title">暂无分析链路</p>
          <p class="trace-empty-hint">发送分析请求后，AI 的计划任务、工具调用、图表与报告会实时展示在这里</p>
        </div>
        <AnalysisTimeline
          v-else
          :turns="activeTraceTurns"
          :steps="activeAnalysisTrace"
          :app-id="appId"
        />
      </div>

      <!-- Task Board（跨 Tab 共享） -->
      <ChatTaskBoard v-if="appId && tasks.length" :tasks="tasks" />

      <!-- HITL 审批条：高风险工具执行前贴在输入框上方确认 -->
      <ChatApprovalPanel
        v-if="pendingApproval"
        :pending-approval="pendingApproval"
        :pending-count="pendingCount"
        :submitting="approvalSubmitting"
        @resolve="resolveApproval"
      />

      <!-- 输入框（跨 Tab 共享） -->
      <ChatInputArea
        v-model="userInput"
        v-model:current-db="currentDb"
        :datasets="datasets"
        :generating="isGenerating"
        :stopping="isStoppingGeneration"
        :can-operate="canOperateApp"
        :input-height="inputHeight"
        :placeholder="getInputPlaceholder()"
        @send="sendMessage"
        @stop="stopGeneration"
        @resize-start="onResizeStart('input', $event)"
      />
    </div>

    <!-- Create File Modal -->
    <a-modal v-model:open="createFileModalVisible" title="新建文件" :mask-closable="false">
      <a-input v-model:value="newItemName" placeholder="输入文件名，如 index.html" @keydown.enter="doCreateFile" />
      <div v-if="newItemParentPath" style="margin-top: 8px; color: var(--text-muted); font-size: 12px;">
        创建位置：{{ newItemParentPath }}
      </div>
      <template #footer>
        <a-button @click="createFileModalVisible = false">取消</a-button>
        <a-button type="primary" :loading="fileOpLoading" @click="doCreateFile">创建</a-button>
      </template>
    </a-modal>

    <!-- Create Folder Modal -->
    <a-modal v-model:open="createFolderModalVisible" title="新建文件夹" :mask-closable="false">
      <a-input v-model:value="newItemName" placeholder="输入文件夹名，如 components" @keydown.enter="doCreateFolder" />
      <div v-if="newItemParentPath" style="margin-top: 8px; color: var(--text-muted); font-size: 12px;">
        创建位置：{{ newItemParentPath }}
      </div>
      <template #footer>
        <a-button @click="createFolderModalVisible = false">取消</a-button>
        <a-button type="primary" :loading="fileOpLoading" @click="doCreateFolder">创建</a-button>
      </template>
    </a-modal>

    <!-- Rename Modal -->
    <a-modal v-model:open="renameModalVisible" title="重命名" :mask-closable="false">
      <a-input v-model:value="renameNewName" placeholder="输入新名称" @keydown.enter="doRename" />
      <template #footer>
        <a-button @click="renameModalVisible = false">取消</a-button>
        <a-button type="primary" :loading="fileOpLoading" @click="doRename">确定</a-button>
      </template>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { useOpenedChatsStore } from '@/stores/openedChats'
import { getAppVoById } from '@/api/appController'

import AnalysisTimeline from '@/components/AnalysisTimeline.vue'
import ChatMessageList from './components/ChatMessageList.vue'
import ChatSessionDropdown from './components/ChatSessionDropdown.vue'
import ChatTaskBoard from './components/ChatTaskBoard.vue'
import ChatApprovalPanel from './components/ChatApprovalPanel.vue'
import ChatInputArea from './components/ChatInputArea.vue'

// 必须在 VueMonacoEditor 之前 import，确保 loader.config({ monaco }) 先执行
import '@/config/monacoWorkers'
import { VueMonacoEditor } from '@guolao/vue-monaco-editor'

import {
  CaretRightOutlined,
  ClearOutlined,
  CloseOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  FileAddOutlined,
  FileTextOutlined,
  FolderAddOutlined,
  FolderOutlined,
  HistoryOutlined,
  PlusOutlined,
  UploadOutlined,
} from '@ant-design/icons-vue'

import { usePanelResize } from './composables/usePanelResize'
import { useChatMessages } from './composables/useChatMessages'
import { useChatSessions } from './composables/useChatSessions'
import { useTraceTimeline } from './composables/useTraceTimeline'
import { useTaskBoard } from './composables/useTaskBoard'
import { useApproval } from './composables/useApproval'
import { useChatInput } from './composables/useChatInput'
import { useElementSelection } from './composables/useElementSelection'
import { useEditorTabs } from './composables/useEditorTabs'
import { useScriptRunner } from './composables/useScriptRunner'
import { useSourceTree } from './composables/useSourceTree'
import { useChatStream } from './composables/useChatStream'
import { createClientId, getAppSessionStorageKey } from './composables/chatHelpers'

// 聊天页在 BasicLayout 工作区以多实例常驻（每个项目一个实例），appId/active 由父组件传入
const props = defineProps<{ appId?: string; active?: boolean }>()

const router = useRouter()
const openedChatsStore = useOpenedChatsStore()

const appInfo = ref<API.AppVO>({})
const appId = ref<string>()

// 后端 get/vo 已校验参与者身份（owner/成员/管理员），能加载出项目即可操作
const canOperateApp = computed(() => Boolean(appId.value))

// ---- 组装各功能域 composable（跨域共享状态用 refs 显式传参，无全局单例）----

// 面板拖拽调宽/调高（chatWidth 会被时间线域引用）
const { sidePanelWidth, chatWidth, outputHeight, inputHeight, onResizeStart } = usePanelResize()

// 当前生成中的 AI 消息下标：消息域与流处理域共享定位
const activeGenerationMessageIndex = ref<number | null>(null)
const chatMsgs = useChatMessages(activeGenerationMessageIndex)

// 会话管理（新建会话时联动清空任务看板）
const chatSessions = useChatSessions({
  appId,
  messages: chatMsgs.messages,
  onSessionCreated: () => taskBoard.resetTasks(),
})

// 时间线（sessionId 共享；切"分析链路"Tab 时自动加宽聊天栏）
const timeline = useTraceTimeline(chatSessions.sessionId, chatWidth)

// 任务看板 / 审批弹窗 / 输入区 / 网页元素选择
const taskBoard = useTaskBoard(timeline.upsertTaskNode)
const approval = useApproval(appId)
const chatInput = useChatInput()
const elementSelection = useElementSelection(canOperateApp)

// 代码工作区：编辑器页签 → 脚本运行 → 源码树（删除/重命名时回调关闭相关页签）
const editorTabs = useEditorTabs(appId, canOperateApp)
const scriptRunner = useScriptRunner(appId, editorTabs.activeTabData)
const sourceTree = useSourceTree({
  appId,
  closeTabOnRename: editorTabs.closeTabOnRename,
  closeTabsForDeletedPath: editorTabs.closeTabsForDeletedPath,
})

// SSE 流处理（各功能域经参数注入）
const chatStream = useChatStream({
  appId,
  appInfo,
  currentDb: chatInput.currentDb,
  messages: chatMsgs.messages,
  userInput: chatInput.userInput,
  activeGenerationMessageIndex,
  canOperateApp,
  timeline,
  messagesApi: chatMsgs,
  applyTaskData: taskBoard.applyTaskData,
  showApproval: approval.showApproval,
  clearApproval: approval.clearApproval,
  ensureChatSessionId: chatSessions.ensureChatSessionId,
  reloadSourceTree: sourceTree.loadSourceTree,
  elementSelection,
  appendOutput: scriptRunner.appendOutput,
})

// ---- 模板所需顶层绑定（解构保持 ref 响应性）----
const { messages, messagesContainer } = chatMsgs
const { sessionHistoryVisible, sessionList, loadingSessionHistory, createNewSession, toggleSessionHistory, loadSession } = chatSessions
const { chatTab, activeTraceTurns, activeAnalysisTrace, jumpToTraceStep } = timeline
const { tasks } = taskBoard
const { pendingApproval, pendingCount, approvalSubmitting, resolveApproval } = approval
const { userInput, datasets, currentDb } = chatInput
const { selectedElementInfo, clearSelectedElement } = elementSelection
const {
  fileTabs, activeFileTab, activeTabData, showLoading, editorTheme, editorOptions,
  handleEditorMount, activateFileTab, closeFileTab,
} = editorTabs
const { selectedPythonEnv, pythonEnvOptions, runningScript, outputLines, outputContainer, clearOutput, runScript } = scriptRunner
const {
  sidePanelTab, sidePanelVisible, sourceFileTree, dbTables, loadingDbTables, dbTableTree,
  loadingSourceTree, selectedFileNode, selectedNodeKey, switchSidePanel,
  openCreateFileModal, openCreateFolderModal, doCreateFile, doCreateFolder,
  openRenameModal, doRename, triggerFileUpload, downloadSelectedFile, deleteSelectedNode,
  createFileModalVisible, createFolderModalVisible, newItemName, newItemParentPath,
  fileOpLoading, renameModalVisible, renameNewName,
} = sourceTree
const { isGenerating, isStoppingGeneration, sendMessage, stopGeneration, abortController } = chatStream

// 侧栏树节点事件（页面负责源码树域与页签域的联动转发）
// 下载按钮仅在选中文件（非文件夹）时可用
const isFileNodeSelected = computed(() => selectedFileNode.value?.dataRef?.type === 'file')

const handleTreeNodeSelect = (_keys: string[], { node }: any) => {
  sourceTree.selectTreeNode(node)
}

const handleSourceFileDoubleClick = async (_e: MouseEvent, node: any) => {
  if (!node.isLeaf) return
  await editorTabs.openFileInTab(node.key, node.title)
}

// 输入框占位提示
const getInputPlaceholder = () => {
  if (!appId.value) return '请先创建项目，再开始对话'
  if (!canOperateApp.value) return elementSelection.readOnlyTooltip
  if (selectedElementInfo.value) return `正在编辑 ${selectedElementInfo.value.tagName.toLowerCase()} 元素，描述您想要的修改...`
  return '请描述你的需求，越详细效果越好哦'
}

// 项目信息加载、会话恢复与子资源初始化
const fetchAppInfo = async () => {
  const id = props.appId
  if (!id) return
  appId.value = id; chatSessions.ensureChatSessionId(id)
  try {
    const res = await getAppVoById({ id })
    if (res.data.code === 0 && res.data.data) {
      appInfo.value = res.data.data
      // 同步页签名称
      openedChatsStore.renameTab(id, res.data.data.appName || '')
      if (messages.value.length === 0) {
        // 尝试恢复本地存储的 session
        const storageKey = getAppSessionStorageKey(id)
        const storedSid = localStorage.getItem(storageKey)?.trim()
        if (storedSid && await chatSessions.checkSessionAlive(storedSid)) {
          // session 还活着 → 加载历史，接着聊
          await chatSessions.loadSession(storedSid)
        } else {
          // session 已过期或不存在 → 清理旧ID，新session
          if (storedSid) localStorage.removeItem(storageKey)
          const newSid = createClientId()
          localStorage.setItem(storageKey, newSid)
          chatSessions.sessionId.value = newSid
        }
      }
      await sourceTree.loadSourceTree()
      await sourceTree.loadDbTables()
      await chatInput.fetchDatasets()
      return
    }
    handleAppLoadFailure()
  } catch (error) {
    console.error('获取项目信息失败：', error)
    handleAppLoadFailure()
  }
}

// 项目不存在/无权限：关自己的页签；只有失败的是当前激活页签才导航，避免把别的页签里的用户拽走
const handleAppLoadFailure = () => {
  if (!appId.value) return
  const wasActive = openedChatsStore.activeAppId === appId.value
  const newActive = openedChatsStore.closeChat(appId.value)
  if (wasActive) {
    message.error('项目不存在或无权限访问')
    router.replace(newActive ? `/app/chat/${newActive}` : '/')
  }
}

// 页签激活时：新消息滚到底部、Monaco 重排（v-show 切换无生命周期钩子，靠 prop 感知）
watch(
  () => props.active,
  async (val) => {
    if (!val) return
    await nextTick()
    chatMsgs.scrollToBottom()
    editorTabs.monacoEditor.value?.layout()
  },
)

onMounted(() => {
  sidePanelTab.value = 'files'
  fetchAppInfo()
})

onBeforeUnmount(() => {
  abortController.value?.abort()
})
</script>

<style scoped src="./AppChatPage.css"></style>
