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
      <div v-if="sessionHistoryVisible" class="session-dropdown">
        <a-spin :spinning="loadingSessionHistory">
          <div v-if="!sessionList.length && !loadingSessionHistory" class="session-empty">
            暂无历史会话
          </div>
          <div
            v-for="sess in sessionList"
            :key="sess.session_id"
            class="session-item"
            @click="loadSession(sess.session_id)"
          >
            <div class="session-item-text">{{ sess.first_message }}</div>
          </div>
        </a-spin>
      </div>

      <!-- 对话内容区 -->
      <div v-show="chatTab === 'chat'" class="chat-content">
        <div class="messages-container" ref="messagesContainer">
          <div v-for="(messageItem, index) in messages" :key="index" class="message-item">
            <div v-if="messageItem.type === 'user'" class="user-message">
              <div class="message-bubble user-bubble">
                {{ messageItem.content }}
              </div>
              <div class="message-avatar">
                <a-avatar :size="28">
                  {{ loginUserStore.loginUser.userName?.charAt(0) || 'U' }}
                </a-avatar>
              </div>
            </div>
            <div v-else class="ai-message">
              <div class="message-avatar">
                <a-avatar :src="aiAvatar" :size="28" />
              </div>
              <div class="ai-content">
                <template v-if="messageItem.items?.length">
                  <template v-for="(item, ii) in messageItem.items" :key="ii">
                    <MarkdownRenderer v-if="item.type === 'text'" :content="item.text || ''" />
                    <div v-else-if="item.type === 'step' && item.step" class="ai-step-inline" :class="'step-' + item.step.state">
                      <span class="step-icon">
                        <LoadingOutlined v-if="item.step.state === 'running'" spin />
                        <CheckCircleOutlined v-else-if="item.step.state === 'completed'" />
                        <CloseCircleOutlined v-else />
                      </span>
                      <span class="step-desc">{{ item.step.description }}</span>
                      <span v-if="item.step.detail" class="step-detail">{{ item.step.detail }}</span>
                    </div>
                    <div v-else-if="item.type === 'tool' && item.toolResult" class="tool-result-inline">
                      <span class="tool-icon"><CodeOutlined /></span>
                      <span class="tool-name">{{ item.toolResult.name }}</span>
                      <span v-if="item.toolResult.detail" class="tool-detail">{{ item.toolResult.detail }}</span>
                    </div>
                    <!-- SQL 卡片：穿插在 AI 流式回复中，点击跳到分析链路对应步骤 -->
                    <div
                      v-else-if="item.type === 'sql-card' && item.sqlCard"
                      class="sql-card-message"
                      :class="{ 'sql-failed': !item.sqlCard.success }"
                      @click="jumpToTraceStep(item.sqlCard.stepId)"
                    >
                      <span class="sql-card-icon">🗄️</span>
                      <div class="sql-card-body">
                        <div class="sql-card-title">
                          SQL 查询
                          <span v-if="item.sqlCard.success" class="sql-card-meta">
                            {{ item.sqlCard.rowCount ?? 0 }} 行 · {{ item.sqlCard.durationMs ?? 0 }}ms
                          </span>
                          <span v-else class="sql-card-meta sql-card-meta-fail">执行失败</span>
                        </div>
                        <div class="sql-card-snippet">{{ item.sqlCard.snippet }}</div>
                      </div>
                      <span class="sql-card-jump">→</span>
                    </div>
                  </template>
                </template>
                <template v-else>
                  <MarkdownRenderer v-if="messageItem.content" :content="messageItem.content" />
                  <div v-if="messageItem.steps?.length" class="ai-steps">
                    <div v-for="(step, si) in messageItem.steps" :key="si" class="ai-step" :class="'step-' + step.state">
                      <span class="step-icon">
                        <LoadingOutlined v-if="step.state === 'running'" spin />
                        <CheckCircleOutlined v-else-if="step.state === 'completed'" />
                        <CloseCircleOutlined v-else />
                      </span>
                      <span class="step-desc">{{ step.description }}</span>
                      <span v-if="step.detail" class="step-detail">{{ step.detail }}</span>
                    </div>
                  </div>
                </template>
                <div v-if="messageItem.loading" class="loading-indicator">
                  <a-spin size="small" />
                  <span>AI 正在思考...</span>
                </div>
              </div>
            </div>
          </div>
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

      <!-- 分析链路内容区 -->
      <div v-show="chatTab === 'trace'" class="trace-container">
        <div v-if="!activeAnalysisTrace.length" class="trace-empty">
          <div class="trace-empty-icon">🔍</div>
          <p class="trace-empty-title">暂无分析链路</p>
          <p class="trace-empty-hint">发送分析请求后，AI 执行的 SQL、可视化和报告会实时展示在这里</p>
        </div>
        <div v-else class="trace-timeline">
          <div
            v-for="step in activeAnalysisTrace"
            :key="step.id"
            :id="'trace-step-' + step.id"
            class="trace-step-wrapper"
          >
            <AnalysisStepCard :step="step" />
          </div>
        </div>
      </div>

      <!-- Task Board（跨 Tab 共享） -->
      <div v-if="appId && tasks.length" class="task-board" :class="{ collapsed: taskBoardCollapsed }">
        <div class="task-board-header" @click="taskBoardCollapsed = !taskBoardCollapsed">
          <span class="task-board-title">
            <CheckSquareOutlined />
            <span>任务 {{ visibleTasks.length ? '(' + visibleTasks.filter(t => t.status === 'completed').length + '/' + visibleTasks.length + ')' : '' }}</span>
          </span>
          <span class="task-board-toggle">
            <UpOutlined v-if="!taskBoardCollapsed" />
            <DownOutlined v-else />
          </span>
        </div>
        <div v-show="!taskBoardCollapsed" class="task-board-body">
          <div v-if="!visibleTasks.length" class="task-board-empty">暂无任务</div>
          <div
            v-for="task in visibleTasks"
            :key="task.id"
            class="task-chip"
            :class="'task-' + task.status"
          >
            <span class="task-chip-indicator">
              <CheckCircleOutlined v-if="task.status === 'completed'" />
              <LoadingOutlined v-else-if="task.status === 'in_progress'" />
              <ClockCircleOutlined v-else />
            </span>
            <span class="task-chip-subject">{{ task.subject }}</span>
            <span class="task-chip-id">#{{ task.id }}</span>
            <span v-if="task.blockedBy.length" class="task-chip-blocked">{{ task.blockedBy.join(',') }}</span>
          </div>
        </div>
      </div>

      <!-- 输入框（跨 Tab 共享） -->
      <div class="input-container" :style="{ height: inputHeight + 'px' }">
        <div class="resizer-horizontal input-resizer" @mousedown="onResizeStart('input', $event)" />
        <div class="input-wrapper">
          <a-textarea
            v-model:value="userInput"
            :placeholder="getInputPlaceholder()"
            :maxlength="1000"
            @keydown.enter.prevent="sendMessage"
            :disabled="isGenerating || !canOperateApp"
            class="chat-input"
          />
          <!-- 数据集选择：默认未选择（agent 分不清时会反问） -->
          <div v-if="datasets.length" class="input-toolbar">
            <a-select
              v-model:value="currentDb"
              :options="datasets"
              :placeholder="'未选择数据集'"
              :disabled="isGenerating || !canOperateApp"
              allow-clear
              size="small"
              class="dataset-select"
            />
          </div>
          <div class="input-actions">
            <a-button v-if="isGenerating" danger @click="stopGeneration" :loading="isStoppingGeneration" size="small">
              <template #icon><StopOutlined /></template>
            </a-button>
            <a-button v-else type="primary" @click="sendMessage" :disabled="!canOperateApp" size="small">
              <template #icon><SendOutlined /></template>
            </a-button>
          </div>
        </div>
      </div>
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
import { message, Modal } from 'ant-design-vue'
import { useLoginUserStore } from '@/stores/loginUser'
import { useOpenedChatsStore } from '@/stores/openedChats'
import { getAppVoById } from '@/api/appController'
import request from '@/request'

import MarkdownRenderer from '@/components/MarkdownRenderer.vue'
import AnalysisStepCard from '@/components/AnalysisStepCard.vue'
import aiAvatar from '@/assets/aiAvatar.png'
import { API_BASE_URL } from '@/config/env'
import { VisualEditor, type ElementInfo } from '@/utils/visualEditor'
// 必须在 VueMonacoEditor 之前 import，确保 loader.config({ monaco }) 先执行
import '@/config/monacoWorkers'
import { VueMonacoEditor, type MonacoEditor } from '@guolao/vue-monaco-editor'

import {
  CaretRightOutlined,
  CheckCircleOutlined,
  CheckSquareOutlined,
  ClearOutlined,
  ClockCircleOutlined,
  CloseCircleOutlined,
  CloseOutlined,
  DatabaseOutlined,
  CodeOutlined,
  DeleteOutlined,
  DownOutlined,
  EditOutlined,
  FileAddOutlined,
  FileTextOutlined,
  FolderAddOutlined,
  FolderOutlined,
  HistoryOutlined,
  LoadingOutlined,
  PlusOutlined,
  QuestionCircleOutlined,
  SendOutlined,
  StopOutlined,
  UpOutlined,
  UploadOutlined,
} from '@ant-design/icons-vue'

interface AiStep {
  eventType: string
  description: string
  detail: string
  state: 'running' | 'completed' | 'failed'
  timestamp: number
  toolId?: string
}

interface ItemEntry {
  type: 'text' | 'step' | 'tool' | 'sql-card'
  text?: string
  step?: AiStep
  toolResult?: { name: string; detail: string }
  // sql-card：与分析链路 step.id 关联，点击可跳转定位
  sqlCard?: { stepId: string; snippet: string; rowCount?: number; durationMs?: number; success: boolean }
}

interface TaskInfo {
  id: number
  subject: string
  description: string
  status: 'pending' | 'in_progress' | 'completed' | 'deleted'
  blockedBy: number[]
  blocks: number[]
  owner: string
}

interface MessageItem {
  type: 'user' | 'ai'
  content: string
  loading?: boolean
  createTime?: string
  steps?: AiStep[]
  items?: ItemEntry[]
}

interface FileTab {
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

interface OutputLine {
  type: 'stdout' | 'stderr' | 'system'
  text: string
  timestamp: number
}

// 分析链路步骤（右栏"分析链路"Tab 用，按 session 内存缓存，后端不保留）
interface AnalysisStep {
  id: string
  type: 'sql' | 'chart' | 'report'
  title: string
  status: 'running' | 'completed' | 'failed'
  timestamp: number
  // sql 专属
  sql?: string
  rowCount?: number
  durationMs?: number
  rowsPreview?: Record<string, any>[]  // SQL 执行结果前 N 行（后端 extras.rows_preview）
  // chart 专属（echarts option）
  chartOption?: any
  chartType?: string
  // report 专属
  markdown?: string
  // 通用错误信息
  error?: string
}

const MAX_OPEN_TABS = 5

// 聊天页在 BasicLayout 工作区以多实例常驻（每个项目一个实例），appId/active 由父组件传入
const props = defineProps<{ appId?: string; active?: boolean }>()

const router = useRouter()
const loginUserStore = useLoginUserStore()
const openedChatsStore = useOpenedChatsStore()

const appInfo = ref<API.AppVO>({})
const appId = ref<string>()
const sessionId = ref('')

// 可选数据集（后端 /api/ai/datasets 返回）；为空时不展示选择器
const datasets = ref<Array<{ value: string; label: string }>>([])
const currentDb = ref<string | undefined>(undefined)

// 分析链路：按 session 内存缓存（页面刷新后清空，后端不持久化）
const analysisTraceMap = ref<Map<string, AnalysisStep[]>>(new Map())
const activeAnalysisTrace = computed<AnalysisStep[]>(() => {
  const sid = sessionId.value
  if (!sid) return []
  let arr = analysisTraceMap.value.get(sid)
  if (!arr) {
    arr = []
    analysisTraceMap.value.set(sid, arr)
  }
  return arr
})

// 右栏 Tab 切换：对话 / 分析链路
const chatTab = ref<'chat' | 'trace'>('chat')
// 记录用户手动拖宽前的默认宽度，切到 trace 自动加宽、切回恢复
const DEFAULT_CHAT_WIDTH = 320
const TRACE_CHAT_WIDTH = 480
let savedChatWidth = DEFAULT_CHAT_WIDTH

const messages = ref<MessageItem[]>([])
const userInput = ref('')
const isGenerating = ref(false)
const isStoppingGeneration = ref(false)
const messagesContainer = ref<HTMLElement>()
const activeGenerationRequestId = ref('')
const activeGenerationSessionId = ref('')
const activeGenerationMessageIndex = ref<number | null>(null)
const stopRequested = ref(false)
const abortController = ref<AbortController | null>(null)

// Resizable panels
const sidePanelWidth = ref(260)
const chatWidth = ref(320)
const outputHeight = ref(150)
const inputHeight = ref(100)
const resizing = ref<'sidebar' | 'chat' | 'output' | 'input' | null>(null)
const outputContainer = ref<HTMLElement>()

// Monaco editor state
const editorTheme = ref<'vs' | 'vs-dark'>('vs')
const monacoEditor = ref<MonacoEditor | null>(null)
const editorMounted = ref(false)
const outputLines = ref<OutputLine[]>([])

const showLoading = computed(() => {
  const tab = activeTabData.value
  const shouldShow = !!tab?.isLoading
  if (tab) {
    console.log(`[ShowLoading] Tab: ${tab.path}, isLoading: ${tab.isLoading}, shouldShow: ${shouldShow}`)
  }
  return shouldShow
})

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

const onResizeStart = (target: 'sidebar' | 'chat' | 'output', e: MouseEvent) => {
  resizing.value = target
  document.addEventListener('mousemove', onResizeMove)
  document.addEventListener('mouseup', onResizeEnd)
  e.preventDefault()
}

const onResizeMove = (e: MouseEvent) => {
  if (resizing.value === 'sidebar') {
    const w = Math.max(200, Math.min(400, e.clientX))
    sidePanelWidth.value = w
  } else if (resizing.value === 'chat') {
    const w = Math.max(260, Math.min(600, window.innerWidth - e.clientX))
    chatWidth.value = w
  } else if (resizing.value === 'output') {
    const centerEl = document.querySelector('.center-column') as HTMLElement
    if (!centerEl) return
    const rect = centerEl.getBoundingClientRect()
    const h = Math.max(80, Math.min(rect.height * 0.5, rect.bottom - e.clientY))
    outputHeight.value = h
  } else if (resizing.value === 'input') {
    const chatPanel = document.querySelector('.right-chat-panel') as HTMLElement
    if (!chatPanel) return
    const rect = chatPanel.getBoundingClientRect()
    const h = Math.max(80, Math.min(300, rect.bottom - e.clientY))
    inputHeight.value = h
  }
}

const onResizeEnd = () => {
  resizing.value = null
  document.removeEventListener('mousemove', onResizeMove)
  document.removeEventListener('mouseup', onResizeEnd)
}

const isEditMode = ref(false)
const selectedElementInfo = ref<ElementInfo | null>(null)
const visualEditor = new VisualEditor({
  onElementSelected: (elementInfo: ElementInfo) => {
    selectedElementInfo.value = elementInfo
  },
})

const readOnlyTooltip = '无法在别人的项目下操作哦~'

const sidePanelTab = ref<'data' | 'files'>('files')
const sidePanelVisible = ref(true)
const sourceFileTree = ref<any[]>([])
const sourceRawNodes = ref<any[]>([])
const loadingSourceTree = ref(false)

const fileTabs = ref<FileTab[]>([])
const activeFileTab = ref<string>('')
const selectedFileNode = ref<any>(null)
const selectedNodeKey = ref<string>('')

const activeTabData = computed(() => {
  return fileTabs.value.find(t => t.path === activeFileTab.value)
})

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

const removeNodeFromRaw = (targetPath: string) => {
  const walk = (nodes: any[]): boolean => {
    const idx = nodes.findIndex(n => n.path === targetPath)
    if (idx >= 0) { nodes.splice(idx, 1); return true }
    for (const n of nodes) {
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

const switchToFilesTab = async () => {
  sidePanelTab.value = 'files'
  if (!sourceFileTree.value.length) {
    await loadSourceTree()
  }
}

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
    let oldest = fileTabs.value[0]
    for (let i = 1; i < fileTabs.value.length; i++) {
      if (fileTabs.value[i].lastAccessed < oldest.lastAccessed) {
        oldest = fileTabs.value[i]
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
      fileTabs.value[idx] = { ...fileTabs.value[idx] }
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

const closeFileTab = (path: string) => {
  const idx = fileTabs.value.findIndex(t => t.path === path)
  if (idx === -1) return

  const tab = fileTabs.value[idx]
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

const doCloseTab = (idx: number, path: string) => {
  fileTabs.value.splice(idx, 1)

  if (activeFileTab.value === path) {
    if (fileTabs.value.length > 0) {
      let mostRecent = fileTabs.value[0]
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

const handleTreeNodeSelect = (_keys: string[], { node }: any) => {
  selectedFileNode.value = node
  selectedNodeKey.value = node.key
}

const handleSourceFileDoubleClick = async (_e: MouseEvent, node: any) => {
  if (!node.isLeaf) return
  await openFileInTab(node.key, node.title)
}

// File operations
const createFileModalVisible = ref(false)
const createFolderModalVisible = ref(false)
const newItemName = ref('')
const newItemParentPath = ref('')
const fileUploadInput = ref<HTMLInputElement>()
const fileOpLoading = ref(false)

// Python env & script running
const selectedPythonEnv = ref('model')
const pythonEnvOptions = [
  { value: 'model', label: 'model' },
]
const runningScript = ref(false)

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
      const oldTabIdx = fileTabs.value.findIndex(t => t.path === node.key)
      if (oldTabIdx >= 0) {
        fileTabs.value.splice(oldTabIdx, 1)
        if (activeFileTab.value === node.key) {
          activeFileTab.value = fileTabs.value.length > 0 ? fileTabs.value[Math.min(oldTabIdx, fileTabs.value.length - 1)].path : ''
        }
      }
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
          const deletedPath = node.key
          const deletedIsDir = node.dataRef?.type === 'dir'
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
            activeFileTab.value = fileTabs.value.length > 0 ? fileTabs.value[fileTabs.value.length - 1].path : ''
          }

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

const clearOutput = () => {
  outputLines.value = []
}

const appendOutput = (type: OutputLine['type'], text: string) => {
  outputLines.value.push({ type, text, timestamp: Date.now() })
  if (outputContainer.value) {
    outputContainer.value.scrollTop = outputContainer.value.scrollHeight
  }
}

const getAuthHeaders = (): Record<string, string> => {
  const token = localStorage.getItem('token')
  if (!token) return {}
  return { Authorization: `Bearer ${token}` }
}

const createClientId = () => {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`
}

const getAppSessionStorageKey = (currentAppId: string) => `codegenx:app-chat-session:${currentAppId}`

const clearChatSessionId = () => { sessionId.value = '' }

// Session history
interface SessionListItem {
  session_id: string
  first_message: string
  create_time: string
}
const sessionHistoryVisible = ref(false)
const loadingSessionHistory = ref(false)
const sessionList = ref<SessionListItem[]>([])
const loadingMessages = ref(false)

const createNewSession = () => {
  if (!appId.value) return
  const storageKey = getAppSessionStorageKey(appId.value)
  localStorage.removeItem(storageKey)
  sessionId.value = createClientId()
  localStorage.setItem(storageKey, sessionId.value)
  messages.value = []
  tasks.value = []
  message.info('已创建新会话')
}

const toggleSessionHistory = async () => {
  if (!appId.value) return
  if (sessionHistoryVisible.value) {
    sessionHistoryVisible.value = false
    return
  }
  sessionHistoryVisible.value = true
  loadingSessionHistory.value = true
  try {
    const baseURL = request.defaults.baseURL || API_BASE_URL
    const res = await fetch(`${baseURL}/api/ai/sessions/${appId.value}?limit=5`, {
      headers: { ...getAuthHeaders() },
    })
    if (res.ok) {
      const result = await res.json()
        sessionList.value = result.data || []
    }
  } catch (e) {
    console.error('获取会话历史失败', e)
  } finally {
    loadingSessionHistory.value = false
  }
}

/**
 * 将 API 返回的 JSONL 消息转换为前端 MessageItem[]。
 * tool 角色消息合并入其前的 assistant 消息 items 中。
 */
const convertMessagesFromApi = (msgs: any[]): MessageItem[] => {
  const items: MessageItem[] = []
  for (const m of msgs) {
    if (m.role === 'user') {
      items.push({ type: 'user', content: m.content || '', createTime: m.create_time })
    } else if (m.role === 'assistant') {
      if (m.content) {
        items.push({ type: 'ai', content: m.content, createTime: m.create_time })
      }
    } else if (m.role === 'tool') {
      const toolName = m.name || 'tool'
      const shortContent = (m.content || '').length > 200
        ? (m.content || '').substring(0, 200) + '...'
        : (m.content || '')
      items.push({
        type: 'ai',
        content: '',
        createTime: m.create_time,
        items: [{
          type: 'tool',
          toolResult: { name: toolName, detail: shortContent },
        }],
      })
    }
  }
  return items
}

const loadSession = async (sid: string) => {
  if (!appId.value) return
  loadingMessages.value = true
  sessionHistoryVisible.value = false
  try {
    const baseURL = request.defaults.baseURL || API_BASE_URL
    const res = await fetch(`${baseURL}/api/ai/sessions/${appId.value}/${sid}/messages?limit=50`, {
      headers: { ...getAuthHeaders() },
    })
    if (!res.ok) throw new Error('load failed')
    const result = await res.json()
    const msgs: any[] = result.data || []

    messages.value = convertMessagesFromApi(msgs)

    // Update localStorage with loaded session id
    const storageKey = getAppSessionStorageKey(appId.value)
    localStorage.setItem(storageKey, sid)
    sessionId.value = sid
    message.success('已加载历史会话')
  } catch (e) {
    console.error('加载会话消息失败', e)
    message.error('加载会话消息失败')
  } finally {
    loadingMessages.value = false
  }
}

const loadLatestSessionMessages = async () => {
  if (!appId.value) return
  try {
    const baseURL = request.defaults.baseURL || API_BASE_URL
    // 获取最近的会话列表
    const sessionsRes = await fetch(`${baseURL}/api/ai/sessions/${appId.value}?limit=1`, {
      headers: { ...getAuthHeaders() },
    })
    if (!sessionsRes.ok) return
    const result = await sessionsRes.json()
    const sessions: SessionListItem[] = result.data || []
    if (!sessions.length) return
    const latestSessionId = sessions[0].session_id
    // 有历史会话则加载消息
    await loadSession(latestSessionId)
  } catch (e) {
    console.error('加载最近会话失败', e)
  }
}

const checkSessionAlive = async (sid: string): Promise<boolean> => {
  if (!appId.value) return false
  try {
    const baseURL = request.defaults.baseURL || API_BASE_URL
    const res = await fetch(`${baseURL}/api/ai/sessions/${appId.value}/${sid}/alive`, {
      headers: { ...getAuthHeaders() },
    })
    if (!res.ok) return false
    const result = await res.json()
    return result.data?.alive === true
  } catch {
    return false
  }
}

const ensureChatSessionId = (currentAppId?: string) => {
  if (!currentAppId) { clearChatSessionId(); return '' }
  const storageKey = getAppSessionStorageKey(currentAppId)
  const storedSessionId = localStorage.getItem(storageKey)?.trim()
  if (storedSessionId) { sessionId.value = storedSessionId; return storedSessionId }
  const createdSessionId = createClientId()
  localStorage.setItem(storageKey, createdSessionId)
  sessionId.value = createdSessionId
  return createdSessionId
}

const getMessageAt = (index: number) => messages.value[index]

const applyMessageChunk = (chunk: unknown) => {
  if (chunk === undefined || chunk === null) return
  const targetMessage = getMessageAt(activeGenerationMessageIndex.value ?? -1)
  if (!targetMessage) return
  targetMessage.content = `${targetMessage.content || ''}${String(chunk)}`
  if (!targetMessage.items) targetMessage.items = []
  const last = targetMessage.items[targetMessage.items.length - 1]
  if (last && last.type === 'text') {
    last.text = (last.text || '') + String(chunk)
  } else {
    targetMessage.items.push({ type: 'text', text: String(chunk) })
  }
  targetMessage.loading = false
  scrollToBottom()
}

const finishStream = () => {
  isGenerating.value = false
  isStoppingGeneration.value = false
  stopRequested.value = false
  if (appId.value) openedChatsStore.setGenerating(appId.value, false)
  clearActiveGeneration(activeGenerationRequestId.value)
  setTimeout(async () => { await refreshAfterGeneration() }, 1000)
}

const refreshAfterGeneration = async () => {
  if (hadFileChangeInGeneration.value) {
    hadFileChangeInGeneration.value = false
    await loadSourceTree()
  }
}

// ----------- 分析链路工具 -----------
const genTraceStepId = () => `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`

// 新增一个 running/completed 的分析步骤
const addTraceStep = (partial: Partial<AnalysisStep> & { type: AnalysisStep['type']; title: string }) => {
  const step: AnalysisStep = {
    id: genTraceStepId(),
    type: partial.type,
    title: partial.title,
    status: 'running',
    timestamp: Date.now(),
    ...partial,
  } as AnalysisStep
  activeAnalysisTrace.value.push(step)
  return step
}

// 更新最后一个 running 的指定 type 步骤（sql/chart/report 执行完更新状态和内容）
const updateLastRunningTraceStep = (type: AnalysisStep['type'], patch: Partial<AnalysisStep>) => {
  const list = activeAnalysisTrace.value
  for (let i = list.length - 1; i >= 0; i--) {
    if (list[i].type === type && list[i].status === 'running') {
      list[i] = { ...list[i], ...patch, status: patch.status ?? 'completed' }
      return list[i]
    }
  }
  return null
}

// ---- SQL 卡片（穿插在 AI 消息 items 里）与分析链路（右栏 trace）联动 ----
const SQL_SNIPPET_LEN = 80
const appendSqlCardItem = (step: AnalysisStep) => {
  // 追加到当前生成中的 AI 消息 items 末尾，按时间穿插在 text/step/tool 之间
  const targetMessage = getMessageAt(activeGenerationMessageIndex.value ?? -1)
  if (!targetMessage) return
  if (!targetMessage.items) targetMessage.items = []
  const sql = step.sql || ''
  const snippet = sql.length > SQL_SNIPPET_LEN ? sql.slice(0, SQL_SNIPPET_LEN) + '…' : sql
  targetMessage.items.push({
    type: 'sql-card',
    sqlCard: {
      stepId: step.id,
      snippet,
      rowCount: step.rowCount,
      durationMs: step.durationMs,
      success: step.status === 'completed',
    },
  })
  scrollToBottom()
}

// 点击 SQL 卡片 → 切到分析链路 Tab 并滚动定位到对应步骤卡片
const jumpToTraceStep = async (stepId?: string) => {
  if (!stepId) return
  chatTab.value = 'trace'
  await nextTick()
  const el = document.getElementById(`trace-step-${stepId}`)
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

// 切右栏 Tab 时自动调宽（只在用户没手动拖过时生效）
watch(chatTab, (tab) => {
  if (tab === 'trace' && chatWidth.value <= DEFAULT_CHAT_WIDTH + 5) {
    savedChatWidth = chatWidth.value
    chatWidth.value = TRACE_CHAT_WIDTH
  } else if (tab === 'chat' && chatWidth.value === TRACE_CHAT_WIDTH) {
    chatWidth.value = savedChatWidth || DEFAULT_CHAT_WIDTH
  }
})

// session 切换时自动清空当前的 analysisTrace（切走再切回来页面已刷新则自然丢失）
watch(sessionId, () => {
  // analysisTraceMap 里按 sessionId 已做了隔离，此处无需清理 Map
})

const appendAiStep = (step: AiStep) => {
  const targetMessage = getMessageAt(activeGenerationMessageIndex.value ?? -1)
  if (!targetMessage) return
  if (!targetMessage.steps) targetMessage.steps = []
  targetMessage.steps.push(step)
  if (!targetMessage.items) targetMessage.items = []
  targetMessage.items.push({ type: 'step', step })
  scrollToBottom()
}

const updateLastRunningToolStep = (detail: string, state: AiStep['state'], toolId?: string) => {
  const targetMessage = getMessageAt(activeGenerationMessageIndex.value ?? -1)
  if (!targetMessage?.steps) return
  for (let i = targetMessage.steps.length - 1; i >= 0; i--) {
    if (targetMessage.steps[i].eventType === 'ToolExecutionStart' && targetMessage.steps[i].state === 'running') {
      if (toolId && targetMessage.steps[i].toolId && targetMessage.steps[i].toolId !== toolId) continue
      targetMessage.steps[i].state = state
      targetMessage.steps[i].detail = detail
      scrollToBottom()
      return
    }
  }
}

const failAllRunningToolSteps = () => {
  const targetMessage = getMessageAt(activeGenerationMessageIndex.value ?? -1)
  if (!targetMessage?.steps) return
  for (const step of targetMessage.steps) {
    if (step.eventType === 'ToolExecutionStart' && step.state === 'running') {
      step.state = 'failed'
      step.detail = '执行中断'
    }
  }
}

const handleStreamEvent = (event: { event_type: string; data: any; state?: string }) => {
  const eventType = event.event_type
  const eventData = event.data

  // ---- 分析链路专用事件（推右栏 trace，不混入 messages）----
  if (eventType === 'SQL_Start') {
    addTraceStep({ type: 'sql', title: 'SQL 查询', sql: eventData?.sql || '', durationMs: undefined })
    return
  }
  if (eventType === 'SQL_End') {
    const step = updateLastRunningTraceStep('sql', {
      sql: eventData?.sql,
      rowCount: eventData?.row_count ?? eventData?.rowCount,
      durationMs: eventData?.duration_ms ?? eventData?.durationMs,
      rowsPreview: eventData?.rows_preview || [],
      status: (eventData?.state === 'success') ? 'completed' : 'failed',
      error: eventData?.error || undefined,
    })
    // 同步往当前 AI 消息的 items 推一张 SQL 卡片（点击可跳到分析链路对应步骤）
    if (step) appendSqlCardItem(step)
    return
  }
  if (eventType === 'Chart_Start') {
    addTraceStep({ type: 'chart', title: eventData?.title || '生成可视化', chartType: eventData?.chart_type || '' })
    return
  }
  if (eventType === 'Chart_End') {
    updateLastRunningTraceStep('chart', {
      chartOption: eventData?.chart_option || eventData?.option,
      chartType: eventData?.chart_type,
      title: eventData?.title || '可视化',
      status: (eventData?.state === 'success') ? 'completed' : 'failed',
      error: eventData?.error || undefined,
    })
    return
  }
  if (eventType === 'Report_Start') {
    addTraceStep({ type: 'report', title: eventData?.title || '生成分析报告', markdown: '' })
    return
  }
  if (eventType === 'Report_End') {
    updateLastRunningTraceStep('report', {
      title: eventData?.title || '分析报告',
      markdown: eventData?.markdown || eventData?.content,
      status: (eventData?.state === 'success') ? 'completed' : 'failed',
      error: eventData?.error || undefined,
    })
    return
  }

  if (eventType === 'LLM_Response_Chunk') { applyMessageChunk(eventData); return }
  if (eventType === 'OnTurnStart') {
    appendAiStep({ eventType, description: '开始处理', detail: '', state: 'completed', timestamp: Date.now() })
    return
  }
  if (eventType === 'LLM_Thinking_Start') {
    const promptTokens = eventData?.prompt_tokens ?? 0
    const messageCount = eventData?.message_count ?? 0
    appendAiStep({ eventType, description: 'AI 思考中', detail: `${promptTokens} tokens, ${messageCount} 条消息`, state: 'completed', timestamp: Date.now() })
    return
  }
  if (eventType === 'ToolExecutionStart') {
    const toolName = eventData?.tool_name || eventData?.name || eventData?.function?.name || 'unknown'
    appendAiStep({ eventType, description: `${toolName}`, detail: '', state: 'running', timestamp: Date.now(), toolId: eventData?.tool_id || '' })
    return
  }
  if (eventType === 'ToolExecutionEnd') {
    const toolName = eventData?.tool_name || eventData?.name || 'unknown'
    const description = eventData?.description || ''
    const toolState = eventData?.state || ''
    const isSuccess = toolState === 'success'
    const detail = isSuccess ? description : (description || `失败: ${toolName}`)
    const toolId = eventData?.tool_id || ''
    // 更新任务面板
    if (eventData?.task_data) {
      const td = eventData.task_data
      if (td.action === 'create') {
        tasks.value.push(td.task as TaskInfo)
      } else if (td.action === 'update') {
        const existingIdx = tasks.value.findIndex(t => t.id === td.task.id)
        if (existingIdx >= 0) {
          tasks.value[existingIdx] = { ...tasks.value[existingIdx], ...td.task }
        }
      }
    }
    const FILE_MODIFY_TOOLS = new Set(['write_file', 'edit_file', 'delete_file', 'bash', 'subagent'])
    if (isSuccess && FILE_MODIFY_TOOLS.has(toolName)) {
      hadFileChangeInGeneration.value = true
    }
    updateLastRunningToolStep(detail, isSuccess ? 'completed' : 'failed', toolId)
    return
  }
  if (eventType === 'CompactEvent') {
    const tokensBefore = eventData?.tokens_before ?? 0
    const tokensAfter = eventData?.tokens_after ?? 0
    const removed = eventData?.messages_removed ?? 0
    appendAiStep({ eventType, description: '上下文压缩', detail: `tokens: ${tokensBefore} → ${tokensAfter}, 移除 ${removed} 条`, state: 'completed', timestamp: Date.now() })
    return
  }
  if (eventType === 'RequestCompleted' || eventType === 'RequestStopped') {
    if (stopRequested.value) {
      const targetMessage = getMessageAt(activeGenerationMessageIndex.value ?? -1)
      if (targetMessage && !targetMessage.content) { targetMessage.content = '已停止本次生成。' }
    }
    finishStream(); return
  }
  if (eventType === 'Error') {
    failAllRunningToolSteps()
    const targetMessage = getMessageAt(activeGenerationMessageIndex.value ?? -1)
    if (targetMessage) {
      targetMessage.content = '执行出错，请稍后重试'
      targetMessage.loading = false
    }
    message.error('执行出错，请稍后重试'); finishStream(); return
  }
  if (eventType === 'Log_Chunk' || eventType === 'Terminal_Output') {
    const text = typeof eventData === 'string' ? eventData : eventData?.text || JSON.stringify(eventData)
    appendOutput('stdout', text)
    return
  }
  console.debug('收到未处理的事件:', eventType, eventData)
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

// 页签激活时：新消息滚到底部、Monaco 重排（v-show 切换无生命周期钩子，靠 prop 感知）
watch(
  () => props.active,
  async (val) => {
    if (!val) return
    await nextTick()
    scrollToBottom()
    monacoEditor.value?.layout()
  },
)

// 后端 get/vo 已校验参与者身份（owner/成员/管理员），能加载出项目即可操作
const canOperateApp = computed(() => Boolean(appId.value))

// Task board state
const tasks = ref<TaskInfo[]>([])
const taskBoardCollapsed = ref(false)
const visibleTasks = computed(() => tasks.value.filter(t => t.status !== 'deleted'))

// Track file-modifying tools for intelligent tree refresh
const hadFileChangeInGeneration = ref(false)

const clearActiveGeneration = (requestId?: string) => {
  if (requestId && activeGenerationRequestId.value && activeGenerationRequestId.value !== requestId) return
  activeGenerationRequestId.value = ''
  activeGenerationSessionId.value = ''
  activeGenerationMessageIndex.value = null
  stopRequested.value = false
  isStoppingGeneration.value = false
}

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

const fetchAppInfo = async () => {
  const id = props.appId
  if (!id) return
  appId.value = id; ensureChatSessionId(id)
  try {
    const res = await getAppVoById({ id: id as unknown as number })
    if (res.data.code === 0 && res.data.data) {
      appInfo.value = res.data.data
      // 同步页签名称
      openedChatsStore.renameTab(id, res.data.data.appName || '')
      if (messages.value.length === 0) {
        // 尝试恢复本地存储的 session
        const storageKey = getAppSessionStorageKey(id)
        const storedSid = localStorage.getItem(storageKey)?.trim()
        if (storedSid && await checkSessionAlive(storedSid)) {
          // session 还活着 → 加载历史，接着聊
          await loadSession(storedSid)
        } else {
          // session 已过期或不存在 → 清理旧ID，新session
          if (storedSid) localStorage.removeItem(storageKey)
          const newSid = createClientId()
          localStorage.setItem(storageKey, newSid)
          sessionId.value = newSid
        }
      }
      await loadSourceTree()
      await loadDbTables()
      await fetchDatasets()
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

const sendMessage = async () => {
  if (!canOperateApp.value || !userInput.value.trim() || isGenerating.value) return
  let outgoingMessage = userInput.value.trim()
  if (selectedElementInfo.value) {
    let elementContext = `\n\n选中元素信息：`
    if (selectedElementInfo.value.pagePath) { elementContext += `\n- 页面路径: ${selectedElementInfo.value.pagePath}` }
    elementContext += `\n- 标签: ${selectedElementInfo.value.tagName.toLowerCase()}\n- 选择器: ${selectedElementInfo.value.selector}`
    if (selectedElementInfo.value.textContent) { elementContext += `\n- 当前内容: ${selectedElementInfo.value.textContent.substring(0, 100)}` }
    outgoingMessage += elementContext
  }
  if (!appId.value) {
    message.info('请先创建项目再开始对话')
    return
  }
  userInput.value = ''
  messages.value.push({ type: 'user', content: outgoingMessage })
  if (selectedElementInfo.value) { clearSelectedElement(); if (isEditMode.value) { toggleEditMode() } }
  const aiMessageIndex = messages.value.length
  messages.value.push({ type: 'ai', content: '', loading: true })
  await nextTick(); scrollToBottom()
  isGenerating.value = true
  await generateCode(outgoingMessage, aiMessageIndex)
}

const generateCode = async (userMessage: string, aiMessageIndex: number) => {
  const currentSessionId = ensureChatSessionId(appId.value)
  const requestId = createClientId()
  activeGenerationRequestId.value = requestId; activeGenerationSessionId.value = currentSessionId
  activeGenerationMessageIndex.value = aiMessageIndex
  stopRequested.value = false; isStoppingGeneration.value = false
  if (appId.value) openedChatsStore.setGenerating(appId.value, true)
  const controller = new AbortController(); abortController.value = controller
  try {
    const baseURL = request.defaults.baseURL || API_BASE_URL
    const url = `${baseURL}/api/ai/chat/gen`
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
      body: JSON.stringify({ appId: String(appId.value), message: userMessage, sessionId: currentSessionId, requestId, traceId: createClientId(), dbName: appInfo.value?.dbName || null, currentDb: currentDb.value || null }),
      signal: controller.signal,
    })
    if (!response.ok) { throw new Error(`HTTP ${response.status}`) }
    const reader = response.body?.getReader()
    if (!reader) { throw new Error('No response body') }
    const decoder = new TextDecoder(); let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n'); buffer = lines.pop() || ''
      for (const line of lines) {
        const trimmed = line.trim()
        if (!trimmed) continue
        try { const event = JSON.parse(trimmed); if (event.event_type) { handleStreamEvent(event) } } catch { /* skip */ }
      }
    }
    if (buffer.trim()) { try { const event = JSON.parse(buffer.trim()); if (event.event_type) { handleStreamEvent(event) } } catch { /* skip */ } }
    finishStream()
  } catch (error: any) {
    if (error.name === 'AbortError') { return }
    handleError(error, aiMessageIndex, requestId)
  } finally { if (abortController.value === controller) { abortController.value = null } }
}

const handleError = (error: unknown, aiMessageIndex: number, requestId?: string) => {
  console.error('执行失败：', error)
  const targetMessage = getMessageAt(aiMessageIndex)
  if (targetMessage) { targetMessage.content = '抱歉，执行中出现了错误，请重试。'; targetMessage.loading = false }
  message.error('执行失败，请重试'); isGenerating.value = false
  if (appId.value) openedChatsStore.setGenerating(appId.value, false)
  clearActiveGeneration(requestId)
}

const stopGeneration = async () => {
  if (!isGenerating.value || isStoppingGeneration.value || !appId.value || !activeGenerationRequestId.value || !activeGenerationSessionId.value) return
  isStoppingGeneration.value = true; stopRequested.value = true
  try {
    abortController.value?.abort()
    const baseURL = request.defaults.baseURL || API_BASE_URL
    await fetch(`${baseURL}/api/ai/chat/stop`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
      body: JSON.stringify({ appId: String(appId.value), sessionId: activeGenerationSessionId.value, requestId: activeGenerationRequestId.value, traceId: createClientId(), reason: 'user-stop' }),
    })
    message.info('已停止生成')
  } catch (error) {
    console.error('停止请求失败：', error)
  } finally {
    finishStream()
  }
}

const scrollToBottom = () => { if (messagesContainer.value) { messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight } }

const toggleEditMode = () => {
  if (!canOperateApp.value) { message.warning(readOnlyTooltip); return }
  message.info('编辑模式需要网页预览，请先点击"预览网页"在新窗口打开后再编辑')
}

const clearSelectedElement = () => { selectedElementInfo.value = null; visualEditor.clearSelection() }

const getInputPlaceholder = () => {
  if (!appId.value) return '请先创建项目，再开始对话'
  if (!canOperateApp.value) return readOnlyTooltip
  if (selectedElementInfo.value) return `正在编辑 ${selectedElementInfo.value.tagName.toLowerCase()} 元素，描述您想要的修改...`
  return '请描述你的需求，越详细效果越好哦'
}

onMounted(() => {
  sidePanelTab.value = 'files'
  fetchAppInfo()
})

onBeforeUnmount(() => {
  abortController.value?.abort()
  document.removeEventListener('mousemove', onResizeMove)
  document.removeEventListener('mouseup', onResizeEnd)
})
</script>

<style scoped>
#appChatPage.ide-layout {
  height: 100%;
  display: flex;
  overflow: hidden;
  background: var(--bg-page);
  gap: 4px;
  padding: 4px;
}

/* ====== Activity Bar ====== */
.activity-bar {
  width: 48px;
  min-width: 48px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  align-items: center;
  background: var(--bg-page);
  border-right: 1px solid var(--border-light);
  padding: 8px 0;
}

.activity-bar-top,
.activity-bar-bottom {
  flex-direction: column;
  align-items: center;
  gap: 4px;
}

.activity-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border-radius: var(--radius-btn);
  color: var(--text-muted);
  font-size: 18px;
  cursor: pointer;
  transition: all 0.2s ease;
  position: relative;
}

.activity-icon:hover {
  color: var(--accent-primary);
  background: var(--accent-primary-light);
}

.activity-icon.active {
  color: var(--accent-primary);
  background: var(--accent-primary-light);
}

.activity-icon.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 24px;
  background: var(--accent-primary);
  border-radius: 0 2px 2px 0;
}

/* ====== Side Panel ====== */
.side-panel {
  min-width: 200px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg-page);
  border-right: 1px solid var(--border-light);
  overflow: hidden;
}

.side-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 12px;
  height: 36px;
  border-bottom: 1px solid var(--border-light);
  flex-shrink: 0;
}

.side-panel-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.side-panel-actions {
  display: flex;
  align-items: center;
  gap: 2px;
}

.side-panel-actions :deep(.ant-btn-text) {
  color: var(--text-muted);
}

.side-panel-actions :deep(.ant-btn-text:hover) {
  color: var(--accent-primary);
  background: var(--accent-primary-light);
}
.side-panel-content {
  flex: 1;
  overflow-y: auto;
  padding: 8px 0;
}

.sidebar-loading {
  display: flex;
  justify-content: center;
  padding: 24px;
}

.sidebar-empty {
  display: flex;
  justify-content: center;
  padding: 24px 16px;
  color: var(--text-muted);
  font-size: 13px;
}

.sidebar-empty p {
  margin: 0;
}

.sidebar-tree {
  padding: 4px 8px;
}

.sidebar-tree :deep(.ant-tree-node-content-wrapper) {
  font-size: 13px;
  border-radius: var(--radius-btn);
  padding: 5px 8px;
  margin: 2px 0;
  transition: all 0.15s ease;
}

.sidebar-tree :deep(.ant-tree-node-content-wrapper:hover) {
  background: var(--accent-primary-light);
}

.sidebar-tree :deep(.ant-tree-node-selected) {
  background: var(--accent-primary-light) !important;
}

/* ====== Resizer (vertical) ====== */
.resizer {
  width: 4px;
  flex-shrink: 0;
  cursor: col-resize;
  background: transparent;
  transition: background 0.2s;
  position: relative;
  z-index: 10;
}

.resizer:hover,
.resizer:active {
  background: var(--accent-primary);
}

/* ====== Center Column ====== */
.center-column {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

/* ====== Center Work Area (Editor) ====== */
.center-work-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  background: var(--bg-surface);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-card);
  overflow: hidden;
}

.file-tab-bar {
  display: flex;
  align-items: center;
  height: 36px;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border-light);
  flex-shrink: 0;
}

.file-tab-bar-left {
  display: flex;
  align-items: center;
  overflow-x: auto;
  flex: 1;
  height: 100%;
}

.file-tab-bar-left::-webkit-scrollbar {
  height: 0;
}

.file-tab-bar-right {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 0 8px;
  flex-shrink: 0;
  border-left: 1px solid var(--border-light);
  height: 100%;
}

.file-tab {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 12px;
  height: 100%;
  font-size: 13px;
  color: var(--text-secondary);
  cursor: pointer;
  border-right: 1px solid var(--border-light);
  border-radius: var(--radius-btn) var(--radius-btn) 0 0;
  transition: background 0.15s;
  user-select: none;
}

.file-tab:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.file-tab.active {
  background: var(--accent-primary-light);
  color: var(--accent-primary);
  font-weight: 500;
}

.file-tab.active::after {
  content: '';
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  height: 2px;
  background: var(--accent-primary);
}

.file-tab-dirty {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent-primary, #2563eb);
  flex-shrink: 0;
}

.file-tab-name {
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.file-tab-close {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border-radius: 3px;
  font-size: 10px;
  opacity: 0;
  transition: opacity 0.1s, background 0.1s;
}

.file-tab:hover .file-tab-close {
  opacity: 0.6;
}

.file-tab-close:hover {
  opacity: 1 !important;
  background: rgba(239, 68, 68, 0.15);
  color: #EF4444;
}

/* ====== Editor Area ====== */
.editor-area {
  flex: 1;
  overflow: hidden;
  display: flex;
}

.monaco-editor-container {
  flex: 1;
  height: 100%;
}

.editor-wrapper {
  flex: 1;
  position: relative;
  overflow: hidden;
}

.editor-loading-overlay {
  position: absolute;
  inset: 0;
  z-index: 10;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  background: var(--bg-surface);
  color: var(--text-muted);
}

.center-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--text-muted);
}

.center-empty-icon {
  font-size: 48px;
  color: var(--border-strong);
}

.center-empty-title {
  margin: 0;
  font-size: 15px;
  font-weight: 500;
  color: var(--text-secondary);
}

.center-empty-hint {
  margin: 0;
  font-size: 13px;
  color: var(--text-muted);
}

.center-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  height: 100%;
  color: var(--text-muted);
}

/* ====== Resizer (horizontal, editor ↔ output) ====== */
.resizer-horizontal {
  height: 4px;
  flex-shrink: 0;
  cursor: row-resize;
  background: transparent;
  transition: background 0.2s;
  position: relative;
  z-index: 10;
}

.resizer-horizontal:hover,
.resizer-horizontal:active {
  background: var(--accent-primary);
}

/* ====== Output Panel ====== */
.output-panel {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg-surface);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-card);
  overflow: hidden;
}

.output-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 12px;
  background: var(--bg-subtle);
  border-bottom: 1px solid var(--border-light);
}

.output-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.output-header :deep(.ant-btn-text) {
  color: var(--text-muted);
  font-size: 12px;
  border-radius: var(--radius-btn);
}

.output-header :deep(.ant-btn-text:hover) {
  color: var(--accent-primary);
  background: var(--accent-primary-light);
}

.output-content {
  flex: 1;
  overflow-y: auto;
  background: var(--bg-page);
  padding: 10px 12px;
  font-family: 'SF Mono', 'Fira Code', Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.5;
}

.output-empty {
  color: var(--text-muted);
  font-style: italic;
}

.output-line {
  white-space: pre-wrap;
  word-break: break-all;
}

.output-stdout {
  color: var(--text-primary);
}

.output-stderr {
  color: #ef4444;
}

.output-system {
  color: var(--accent-primary);
}

/* ====== Right Chat Panel ====== */
.right-chat-panel {
  min-width: 260px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg-surface);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-card);
  overflow: hidden;
}

.chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border-light);
  background: var(--bg-surface);
  flex-shrink: 0;
  position: relative;
}
.chat-header-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}
.chat-header-actions {
  display: flex;
  gap: 2px;
}

.session-dropdown {
  background: var(--bg-surface);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-sm);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
  max-height: 200px;
  overflow-y: auto;
  margin: 0 12px 8px;
  padding: 4px 0;
  flex-shrink: 0;
}

.session-item {
  padding: 10px 12px;
  border: 1px solid var(--border-light);
  border-radius: var(--radius-sm);
  margin-bottom: 8px;
  cursor: pointer;
  transition: background 0.15s;
}
.session-item:hover {
  background: var(--bg-hover);
}
.session-item-text {
  font-size: 13px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-empty {
  text-align: center;
  color: var(--text-muted);
  padding: 12px 0;
  font-size: 13px;
}

.messages-container {
  flex: 1;
  padding: 12px;
  overflow-y: auto;
  scroll-behavior: smooth;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.message-item {
  margin-bottom: 4px;
}

.user-message {
  display: flex;
  justify-content: flex-end;
  align-items: flex-end;
  gap: 8px;
}

.ai-message {
  display: flex;
  justify-content: flex-start;
  align-items: flex-start;
  gap: 8px;
}

.ai-content {
  max-width: 85%;
  min-width: 0;
}

.ai-content > * {
  margin-bottom: 4px;
}

.message-bubble {
  padding: 10px 16px;
  border-radius: var(--radius-card);
  line-height: 1.6;
  font-size: 14px;
  transition: all 0.2s ease;
}

.user-bubble {
  max-width: 80%;
  background: var(--accent-primary);
  border: none;
  color: var(--text-inverse);
  border-bottom-right-radius: 4px;
  box-shadow: var(--shadow-sm);
}

.message-avatar {
  flex-shrink: 0;
}

.message-avatar :deep(.ant-avatar) {
  border: 1px solid var(--border-light);
  border-radius: 50%;
}

.loading-indicator {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-muted);
}

.ai-steps {
  margin-top: 6px;
}

.ai-step {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.5;
}

.ai-step .step-icon {
  flex-shrink: 0;
  font-size: 12px;
  width: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.ai-step.step-running .step-icon {
  color: var(--accent-primary);
}

.ai-step.step-completed .step-icon {
  color: #22c55e;
}

.ai-step.step-failed .step-icon {
  color: #ef4444;
}

.ai-step.step-running .step-desc {
  color: var(--accent-primary);
}

.ai-step .step-detail {
  color: var(--text-muted);
  opacity: 0.7;
  margin-left: auto;
  font-size: 11px;
}

.ai-step-inline {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.5;
  margin: 2px 0;
  opacity: 0.7;
}

.ai-step-inline .step-icon {
  flex-shrink: 0;
  font-size: 12px;
  width: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.ai-step-inline.step-running .step-icon {
  color: var(--accent-primary);
}

.ai-step-inline.step-completed .step-icon {
  color: #22c55e;
}

.ai-step-inline.step-failed .step-icon {
  color: #ef4444;
}

.ai-step-inline.step-running .step-desc {
  color: var(--accent-primary);
}

.ai-step-inline .step-detail {
  color: var(--text-muted);
  opacity: 0.7;
  margin-left: auto;
  font-size: 11px;
}

/* Tool result inline in history */
.tool-result-inline {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 2px 6px;
  margin: 2px 0;
  border-radius: 4px;
  background: var(--bg-page);
  font-size: 11px;
  color: var(--text-muted);
}
.tool-result-inline .tool-icon {
  font-size: 11px;
  color: var(--accent-primary);
  flex-shrink: 0;
}
.tool-result-inline .tool-name {
  font-weight: 500;
  color: var(--text-secondary);
  flex-shrink: 0;
}
.tool-result-inline .tool-detail {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ====== Task Board ====== */
.task-board {
  margin: 0 12px 8px;
  background: var(--bg-page);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-card);
  overflow: hidden;
}

.task-board-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 12px;
  cursor: pointer;
  user-select: none;
  background: var(--bg-subtle);
}

.task-board-header:hover {
  background: var(--bg-hover);
}

.task-board-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}

.task-board-title .anticon {
  font-size: 13px;
  color: var(--accent-primary);
}

.task-board-toggle {
  font-size: 12px;
  color: var(--text-muted);
}

.task-board-body {
  padding: 4px 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 200px;
  overflow-y: auto;
}

.task-chip {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 12px;
  font-size: 12px;
  line-height: 1.6;
  border: 1px solid var(--border-light);
  background: var(--bg-surface);
  color: var(--text-secondary);
}

.task-chip.task-completed {
  opacity: 0.5;
  text-decoration: line-through;
}

.task-chip.task-in_progress {
  border-color: var(--accent-primary);
  color: var(--accent-primary);
}

.task-chip-indicator {
  font-size: 11px;
  flex-shrink: 0;
}

.task-chip.task-completed .task-chip-indicator {
  color: #22c55e;
}

.task-chip.task-in_progress .task-chip-indicator {
  color: var(--accent-primary);
}

.task-chip.task-pending .task-chip-indicator {
  color: var(--text-muted);
}

.task-chip-subject {
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-chip-id {
  font-size: 10px;
  color: var(--text-muted);
  opacity: 0.6;
}

.task-chip-blocked {
  font-size: 10px;
  padding: 0 4px;
  border-radius: 4px;
  background: #fef3c7;
  color: #92400e;
}

.input-container {
  display: flex;
  flex-direction: column;
  padding: 0 12px 12px;
  background: var(--bg-surface);
  flex-shrink: 0;
}

.input-resizer {
  cursor: row-resize;
  height: 4px;
  flex-shrink: 0;
  margin-bottom: 4px;
}

.input-resizer:hover,
.input-resizer:active {
  background: var(--accent-primary);
  border-radius: 2px;
}

.input-wrapper {
  position: relative;
  flex: 1;
  display: flex;
}

.chat-input {
  background: var(--bg-page);
  border: 1px solid var(--border-light);
  color: var(--text-primary);
  font-size: 14px;
  border-radius: var(--radius-card);
  transition: all 0.2s ease;
  resize: none;
  width: 100%;
}
.chat-input:hover {
  border-color: var(--border-deep);
}
.chat-input:focus {
  border-color: var(--accent-primary);
  box-shadow: 0 0 0 3px var(--accent-primary-light);
}

.chat-input :deep(textarea) {
  resize: vertical;
  min-height: 60px;
  padding-bottom: 36px;
}

.input-actions {
  position: absolute;
  bottom: 8px;
  right: 8px;
  display: flex;
  gap: 8px;
}

/* 数据集选择器（输入框底部左侧，与发送按钮同行） */
.input-toolbar {
  position: absolute;
  left: 8px;
  bottom: 8px;
  display: flex;
  align-items: center;
  max-width: calc(100% - 64px);
}

.dataset-select {
  min-width: 170px;
  max-width: 100%;
}

.dataset-select :deep(.ant-select-selector) {
  border-radius: 999px;
  background: var(--bg-surface);
}

/* Send button */
.input-actions :deep(.ant-btn-primary) {
  border-radius: 999px;
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
}

/* ====== Selected Element Alert ====== */
.selected-element-alert {
  margin: 0 16px;
}

.element-tag {
  font-weight: 600;
  color: var(--accent-primary);
}

.element-id {
  color: var(--text-secondary);
  margin-left: 4px;
}

.element-class {
  color: var(--text-muted);
  margin-left: 4px;
}

.element-selector-code {
  background: var(--bg-subtle);
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 12px;
}

/* ====== Responsive ====== */
@media (max-width: 1280px) {
  .right-chat-panel {
    width: 320px;
    min-width: 280px;
  }
}

@media (max-width: 1024px) {
  .side-panel {
    width: 48px;
    min-width: 48px;
  }
  .side-panel .side-panel-title span,
  .side-panel .side-panel-actions {
    display: none;
  }
  .side-panel-header {
    justify-content: center;
    padding: 0 4px;
  }
  .right-chat-panel {
    width: 300px;
    min-width: 260px;
  }
}

/* ---- 右栏头部 Tab（AI助手 | 分析链路）---- */
.chat-header-tabs {
  display: flex;
  align-items: center;
  gap: 8px;
}
.chat-header-tab {
  font-size: 14px;
  font-weight: 500;
  color: var(--text-muted, #8c8c8c);
  cursor: pointer;
  transition: color 0.2s;
  user-select: none;
}
.chat-header-tab:hover { color: var(--text-primary, #262626); }
.chat-header-tab.active { color: var(--text-primary, #262626); font-weight: 600; }
.chat-header-tab-divider { color: var(--border-color, #d9d9d9); user-select: none; }

/* 对话/分析链路内容区：撑满头部与输入框之间的空间 */
.chat-content,
.trace-container {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.trace-container { padding: 8px; overflow-y: auto; }
.trace-timeline { display: flex; flex-direction: column; gap: 0; }
.trace-step-wrapper { scroll-margin-top: 8px; }

/* ---- SQL 卡片（穿插在 AI 消息 items 里） ---- */
.sql-card-message {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  margin: 6px 0;
  background: #f6f9ff;
  border: 1px solid #d6e4ff;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.2s;
}
.sql-card-message:hover { background: #eaf1ff; border-color: #91caff; box-shadow: 0 2px 6px rgba(22, 119, 255, 0.1); }
.sql-card-message.sql-failed { background: #fff1f0; border-color: #ffa39e; }
.sql-card-message.sql-failed:hover { background: #ffe7e5; border-color: #ff7875; }

.sql-card-icon { font-size: 18px; flex-shrink: 0; }
.sql-card-body { flex: 1; min-width: 0; }
.sql-card-title {
  font-size: 12px;
  font-weight: 600;
  color: #262626;
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 2px;
}
.sql-card-meta { font-size: 11px; color: #1677ff; font-weight: 400; }
.sql-card-meta-fail { color: #ff4d4f; }
.sql-card-snippet {
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 11px;
  color: #595959;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sql-card-jump { color: #1677ff; font-size: 14px; flex-shrink: 0; }

.trace-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px 20px;
  color: #8c8c8c;
  text-align: center;
}
.trace-empty-icon { font-size: 36px; margin-bottom: 12px; }
.trace-empty-title { font-size: 14px; font-weight: 500; margin: 0 0 4px; color: #595959; }
.trace-empty-hint { font-size: 12px; margin: 0; line-height: 1.6; }

/* 让 messages-container 占满对话内容区 */
.messages-container { flex: 1; min-height: 0; overflow-y: auto; }

@media (max-width: 768px) {
  #appChatPage.ide-layout {
    min-width: 768px;
    overflow-x: auto;
  }
  .message-bubble {
    max-width: 90%;
  }
}
</style>
