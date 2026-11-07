<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import {
  ArrowRightOutlined,
  MessageOutlined,
  PlusOutlined,
  RocketOutlined,
} from '@ant-design/icons-vue'
import { addApp, listMyAppVoByPage } from '@/api/appController'
import { useLoginUserStore } from '@/stores/loginUser'
import { useOpenedChatsStore } from '@/stores/openedChats'
import { formatRelativeTime, formatTime } from '@/utils/time'

const router = useRouter()
const loginUserStore = useLoginUserStore()
const openedChatsStore = useOpenedChatsStore()

const loading = ref(false)
const apps = ref<API.AppVO[]>([])
const total = ref(0)

const promptExamples = [
  '生成一个销售数据看板，按区域和时间维度展示营收、利润和 Top 10 产品',
  '做一个 A/B 实验分析报告页，展示实验组对照组的核心指标对比和置信区间',
  '帮我搭一个 API 调用量监控面板，展示 QPS、P99 延迟和错误率趋势',
]

const isLoggedIn = computed(() => Boolean(loginUserStore.loginUser.id))
const hasApps = computed(() => apps.value.length > 0)

const fetchMyApps = async () => {
  if (!isLoggedIn.value) {
    apps.value = []
    total.value = 0
    return
  }
  loading.value = true
  try {
    const res = await listMyAppVoByPage({
      pageNum: 1,
      pageSize: 12,
      sortField: 'updateTime',
      sortOrder: 'descend',
    })
    if (res.data.code === 0 && res.data.data) {
      apps.value = res.data.data.records ?? []
      total.value = res.data.data.totalRow ?? 0
      return
    }
    message.error(`获取项目列表失败，${res.data.message ?? '请稍后重试'}`)
  } catch (error) {
    console.error('获取项目列表失败:', error)
    message.error('获取项目列表失败')
  } finally {
    loading.value = false
  }
}

const createModalVisible = ref(false)
const createForm = ref({ appName: '', dbName: '' })
const isCreating = ref(false)

const openCreateChat = (prompt?: string) => {
  if (!isLoggedIn.value) {
    router.push('/user/login')
    return
  }
  createForm.value = { appName: '', dbName: '' }
  createModalVisible.value = true
}

const handleCreateProject = async () => {
  if (!createForm.value.appName.trim()) {
    message.warning('请输入项目名称')
    return
  }
  if (createForm.value.appName.trim().length > 20) {
    message.warning('项目名称不能超过20个字')
    return
  }
  isCreating.value = true
  try {
    const payload: Record<string, any> = {
      appName: createForm.value.appName.trim(),
    }
    const dbName = createForm.value.dbName.trim()
    if (dbName) {
      payload.dbName = 'prj_' + dbName
    }
    const createRes = await addApp(payload)
    if (createRes.data.code !== 0 || !createRes.data.data) {
      message.error(`创建项目失败，${createRes.data.message ?? '请稍后重试'}`)
      return
    }
    createModalVisible.value = false
    // 新建成功不自动进入聊天：项目已建好，避免页签满时出现"项目已创建却打不开"的尴尬；用户从列表点"继续生成"进入
    message.success('项目创建成功')
    await fetchMyApps()
  } catch (error) {
    console.error('创建项目失败:', error)
    message.error('创建项目失败')
  } finally {
    isCreating.value = false
  }
}

const selectPromptExample = (_example: string) => {
  openCreateChat()
}

// 进聊天页统一走这里：先开页签（带项目名避免"加载中"闪烁），超限则提示并留在当前页
const enterChat = (appId?: string | null, appName?: string) => {
  if (!appId) return
  if (!openedChatsStore.openChat(String(appId), appName)) {
    message.warning('最多同时打开3个项目，请先关闭一个页签')
    return
  }
  router.push(`/app/chat/${appId}`)
}

const goToChat = (appId?: string | null) => {
  const app = apps.value.find((a) => String(a.id) === String(appId))
  enterChat(appId ?? undefined, app?.appName ?? undefined)
}

watch(
  () => loginUserStore.loginUser.id,
  () => {
    fetchMyApps()
  },
  { immediate: true },
)
</script>

<template>
  <div id="homePage">
    <!-- Welcome / Guest State -->
    <section v-if="!isLoggedIn" class="welcome-section">
      <div class="welcome-card">
        <div class="welcome-icon">
          <RocketOutlined />
        </div>
        <h2 class="section-title">登录后，这里就是你的<span class="highlight">项目工作台</span></h2>
        <p class="section-description">
          首页会根据你的项目状态展示内容。没有项目时只提示如何创建，有项目后只展示项目卡片列表。
        </p>
        <div class="welcome-actions">
          <a-button type="primary" size="large" @click="router.push('/user/login')" class="cta-btn">
            进入平台
            <template #icon><ArrowRightOutlined /></template>
          </a-button>
        </div>
      </div>
    </section>

    <!-- Loading -->
    <section v-else-if="loading" class="loading-panel">
      <a-spin size="large" />
    </section>

    <!-- No Apps / Create Guide -->
    <section v-else-if="!hasApps" class="create-guide-panel">
      <div class="create-guide-card">
        <div class="guide-icon">
          <PlusOutlined />
        </div>
        <h2 class="section-title">你还没有<span class="highlight">项目</span></h2>
        <p class="section-description">
          点击下方按钮直接进入生成对话。首次发送需求时，系统会自动创建项目并开始生成。
        </p>
        <div class="guide-actions">
          <a-button type="primary" size="large" @click="openCreateChat()" class="cta-btn">
            <template #icon><PlusOutlined /></template>
            创建项目
          </a-button>
        </div>
        <div class="example-list">
          <span class="example-label">可以这样描述你的项目</span>
          <div class="example-tags">
            <span
              v-for="example in promptExamples"
              :key="example"
              class="example-tag"
              @click="selectPromptExample(example)"
            >
              {{ example }}
            </span>
          </div>
        </div>
      </div>
    </section>

    <!-- Apps Grid -->
    <section v-else class="apps-panel">
      <div class="section-header">
        <div>
          <h2 class="section-title">我参与的项目</h2>
          <p class="section-description">当前共 {{ total }} 个项目</p>
        </div>
      </div>

      <div class="app-grid">
        <a-card
          v-for="(app, idx) in apps"
          :key="app.id"
          class="app-card"
          :bordered="false"
          :style="{ animationDelay: `${idx * 60}ms` }"
        >
          <div class="app-body">
            <div class="app-heading">
              <h3 class="app-name">{{ app.appName || '未命名项目' }}</h3>
            </div>

            <p class="app-prompt">
              项目库：{{ app.dbName || '未绑定' }}
            </p>

            <div class="app-meta">
              <span>创建时间：{{ formatTime(app.createTime, 'YYYY-MM-DD') || '未知' }}</span>
              <span>更新时间：{{ formatRelativeTime(app.updateTime) || '刚刚' }}</span>
            </div>

            <div class="app-actions">
              <a-button type="primary" size="small" @click="goToChat(app.id)">
                <template #icon><MessageOutlined /></template>
                继续生成
              </a-button>
            </div>
          </div>
        </a-card>

        <a-card class="app-card add-app-card" :bordered="false" @click="openCreateChat()">
          <div class="add-app-content">
            <div class="add-app-icon">
              <PlusOutlined />
            </div>
            <h3 class="add-app-title">添加项目</h3>
          </div>
        </a-card>
      </div>
    </section>

    <a-modal
      v-model:open="createModalVisible"
      title="新建项目"
      :confirm-loading="isCreating"
      @ok="handleCreateProject"
      ok-text="创建项目"
      cancel-text="取消"
      :mask-closable="false"
    >
      <a-form layout="vertical" style="margin-top: 16px">
        <a-form-item label="项目名称" required>
          <a-input
            v-model:value="createForm.appName"
            placeholder="请输入项目名称"
            :maxlength="20"
            show-count
          />
        </a-form-item>
        <a-form-item label="项目库">
          <a-input-group compact>
            <a-input
              style="
                width: 60px;
                background: var(--bg-subtle);
                color: var(--text-muted);
                text-align: right;
              "
              value="prj_"
              disabled
            />
            <a-input
              v-model:value="createForm.dbName"
              placeholder="自定义库名后缀"
              style="width: calc(100% - 60px)"
            />
          </a-input-group>
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<style scoped>
#homePage {
  min-height: calc(100vh - 56px);
  padding: 56px 32px 64px;
}

.welcome-section,
.apps-panel,
.create-guide-panel {
  max-width: 1200px;
  margin: 0 auto 28px;
}

.create-guide-panel {
  display: flex;
  min-height: calc(100vh - 56px - 200px);
}

/* Welcome Card  background: radial-gradient(circle, rgba(99, 102, 241, 0.06) 0%, transparent 70%);*/
.welcome-card {
  text-align: center;
  padding: 80px 48px;
  background: var(--bg-surface);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-container);
  box-shadow: var(--shadow-sm);
}

.welcome-icon {
  font-size: 44px;
  color: var(--accent-primary);
  margin-bottom: 24px;
}

/* Create Guide Card */
.create-guide-card {
  width: 100%;
  min-height: 100%;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  text-align: center;
  padding: 64px 48px;
  background: var(--bg-surface);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-container);
  box-shadow: var(--shadow-sm);
}

.guide-icon {
  font-size: 40px;
  color: var(--accent-primary);
  margin-bottom: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 72px;
  height: 72px;
  border-radius: var(--radius-card);
  background: var(--accent-primary-light);
}

/* Section Eyebrow */
.section-eyebrow {
  margin: 0 0 4px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--accent-primary);
}

/* Section Description */
.section-description {
  margin: 12px 0 0;
  max-width: 560px;
  font-size: 15px;
  line-height: 1.6;
  color: var(--text-secondary);
}

/* Loading */
.loading-panel {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 280px;
}

/* Section Header */
.section-header {
  display: flex;
  justify-content: space-between;
  gap: 20px;
  align-items: flex-start;
  margin-bottom: 24px;
}

/* Section Title */
.section-title {
  margin: 0;
  font-size: 26px;
  font-weight: 700;
  letter-spacing: -0.02em;
  line-height: 1.25;
  color: var(--text-primary);
}

.highlight {
  color: var(--accent-primary);
}

/* Actions */
.welcome-actions,
.guide-actions {
  display: flex;
  justify-content: center;
  margin-top: 24px;
}

.cta-btn {
  height: 44px;
  padding: 0 28px;
  font-size: 15px;
  font-weight: 600;
}

/* App Grid */
.app-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
  gap: 20px;
}

/* App Card */
.app-card {
  overflow: hidden;
  border-radius: var(--radius-card);
  border: 1px solid var(--border-light);
  /* 分层基础阴影：下层模糊大阴影制造浮空基底 */
  box-shadow:
    0 1px 2px rgba(0, 0, 0, 0.06),
    0 2px 4px rgba(0, 0, 0, 0.04),
    0 8px 16px rgba(22, 119, 255, 0.05);
  transition: all 0.25s ease;
  animation: fade-in-up 0.4s var(--ease-out) both;
  background: #ffffff;
}

.app-card:hover {
  transform: translateY(-4px);
  box-shadow: var(--shadow-hover);
  border-color: var(--accent-primary);
}

/* Override Ant Design card padding */
.app-card :deep(.ant-card-body) {
  padding: 12px;
  background: transparent;
}

/* Add App Card */
.add-app-card {
  cursor: pointer;
  min-height: 200px;
  border: 2px dashed var(--border-deep);
  background: var(--bg-page);
  transition: all 0.25s ease;
}
.add-app-card:hover {
  transform: translateY(-4px);
  box-shadow: var(--shadow-hover);
  border-color: var(--accent-primary);
  background: var(--accent-primary-light);
}

.add-app-content {
  height: 100%;
  min-height: 200px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  text-align: center;
}

.add-app-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 48px;
  height: 48px;
  border-radius: var(--radius-card);
  background: var(--accent-primary-light);
  font-size: 22px;
  color: var(--accent-primary);
  transition: all 0.25s ease;
}
.add-app-card:hover .add-app-icon {
  background: var(--accent-primary);
  color: var(--text-inverse);
}

.add-app-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: var(--text-primary);
}

/* App Body */
.app-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 4px;
}

.app-heading {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: flex-start;
}

.app-name {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  line-height: 1.35;
  color: var(--text-primary);
}

.app-prompt {
  min-height: 44px;
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-secondary);
  display: -webkit-box;
  overflow: hidden;
  line-clamp: 2;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.app-meta {
  display: grid;
  gap: 4px;
  font-size: 12px;
  color: var(--text-muted);
}

.app-actions {
  display: flex;
  gap: 8px;
  margin-top: 4px;
}

/* Example Tags */
.example-list {
  display: grid;
  gap: 10px;
  margin-top: 28px;
}

.example-label {
  font-size: 13px;
  color: var(--text-secondary);
}

.example-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
}

.example-tag {
  cursor: pointer;
  padding: 8px 16px;
  border-radius: var(--radius-btn);
  font-size: 13px;
  line-height: 1.5;
  color: var(--text-secondary);
  background: var(--bg-surface);
  border: 1px solid var(--border-light);
  transition: all 0.2s ease;
}
.example-tag:hover {
  color: var(--accent-primary);
  background: var(--accent-primary-light);
  border-color: var(--accent-primary);
}

/* Override Ant Design card padding */
.app-card :deep(.ant-card-body) {
  padding: 16px;
  background: transparent;
}

@media (max-width: 960px) {
  #homePage {
    padding: 24px 20px;
  }
  .create-guide-panel {
    min-height: calc(100vh - 64px - 68px);
  }
  .section-header {
    flex-direction: column;
    align-items: stretch;
  }
  .section-title {
    font-size: 24px;
  }
}

@media (max-width: 640px) {
  .welcome-card,
  .create-guide-card {
    padding: 32px 20px;
  }
  .app-actions {
    flex-direction: column;
  }
  .app-grid {
    grid-template-columns: 1fr;
  }
}
</style>
