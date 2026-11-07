<template>
  <div v-if="visible" class="chat-tabs-bar">
    <div class="chat-tabs">
      <div
        v-for="chat in openedChatsStore.chats"
        :key="chat.appId"
        class="chat-tab"
        :class="{ active: chat.appId === openedChatsStore.activeAppId }"
        :title="chat.appName || chat.appId"
        @click="switchTo(chat.appId)"
      >
        <span
          v-if="openedChatsStore.generatingAppIds.includes(chat.appId)"
          class="gen-dot"
          title="生成中"
        />
        <span class="chat-tab-name">{{ chat.appName || '加载中…' }}</span>
        <span class="chat-tab-close" title="关闭页签" @click.stop="confirmClose(chat)">
          <CloseOutlined />
        </span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Modal } from 'ant-design-vue'
import { CloseOutlined } from '@ant-design/icons-vue'
import { useLoginUserStore } from '@/stores/loginUser'
import { useOpenedChatsStore, type OpenedChat } from '@/stores/openedChats'

const route = useRoute()
const router = useRouter()
const loginUserStore = useLoginUserStore()
const openedChatsStore = useOpenedChatsStore()

const visible = computed(
  () => Boolean(loginUserStore.loginUser.id) && openedChatsStore.chats.length > 0,
)

const switchTo = (appId: string) => {
  if (route.params.id === appId) return
  router.replace(`/app/chat/${appId}`)
}

const confirmClose = (chat: OpenedChat) => {
  const doClose = () => {
    const newActive = openedChatsStore.closeChat(chat.appId)
    // 先改 store 再跳路由：若正在聊天路由内，需导航到相邻页签或首页，避免 watcher 把刚关的页签重新打开
    if (route.name === '项目对话') {
      router.replace(newActive ? `/app/chat/${newActive}` : '/')
    }
  }
  if (openedChatsStore.generatingAppIds.includes(chat.appId)) {
    Modal.confirm({
      title: `关闭「${chat.appName || '项目'}」？`,
      content: '该项目正在生成中，关闭页签将中断生成。',
      okText: '关闭',
      okType: 'danger',
      cancelText: '取消',
      onOk: doClose,
    })
  } else if (openedChatsStore.dirtyAppIds.includes(chat.appId)) {
    Modal.confirm({
      title: `关闭「${chat.appName || '项目'}」？`,
      content: '该项目有未保存的文件更改，关闭页签将丢弃这些更改。',
      okText: '关闭',
      okType: 'danger',
      cancelText: '取消',
      onOk: doClose,
    })
  } else {
    doClose()
  }
}
</script>

<style scoped>
/* 页签并入顶部栏：透明承载、胶囊 chip，不再独占一栏 */
.chat-tabs-bar {
  flex-shrink: 0;
  display: flex;
  align-items: center;
}

.chat-tabs {
  display: flex;
  align-items: center;
  gap: 6px;
  overflow-x: auto;
}

.chat-tabs::-webkit-scrollbar {
  height: 0;
}

.chat-tab {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 12px;
  font-size: 13px;
  color: var(--text-secondary);
  cursor: pointer;
  background: var(--bg-surface);
  border: 1px solid var(--border-light);
  border-radius: 999px;
  transition: all 0.18s ease;
  user-select: none;
  max-width: 200px;
  white-space: nowrap;
}

.chat-tab:hover {
  border-color: var(--accent-primary);
  color: var(--accent-primary);
}

.chat-tab.active {
  background: var(--accent-primary-light);
  border-color: var(--accent-primary);
  color: var(--accent-primary);
  font-weight: 500;
}

.gen-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent-primary);
  flex-shrink: 0;
  animation: gen-pulse 1.2s ease-in-out infinite;
}

@keyframes gen-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.3;
  }
}

.chat-tab-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-tab-close {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  font-size: 10px;
  opacity: 0;
  transition: opacity 0.1s, background 0.1s;
}

.chat-tab:hover .chat-tab-close {
  opacity: 0.6;
}

.chat-tab-close:hover {
  opacity: 1 !important;
  background: rgba(239, 68, 68, 0.15);
  color: #ef4444;
}
</style>
