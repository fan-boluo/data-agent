<template>
  <a-layout class="basic-layout">
    <div class="layout-bg-decor" />
    <GlobalHeader>
      <a-layout-content class="main-content">
      <!-- 聊天工作区：常驻多实例，实例不随路由销毁（v-show 切换），只有关页签才卸载 -->
      <div v-show="isChatRoute" class="chat-workspace">
        <template v-for="chat in openedChatsStore.chats" :key="chat.appId">
          <AppChatPage
            v-if="chat.everActivated"
            v-show="chat.appId === openedChatsStore.activeAppId"
            :key="chat.appId"
            :app-id="chat.appId"
            :active="chat.appId === openedChatsStore.activeAppId"
          />
          <div
            v-else-if="chat.appId === openedChatsStore.activeAppId"
            class="chat-workspace-loading"
          >
            <a-spin size="large" />
          </div>
        </template>
      </div>
      <!-- 常规路由页：聊天路由由工作区接管，这里抑制渲染 -->
      <router-view v-slot="{ Component }">
        <component :is="Component" v-if="!isChatRoute" />
      </router-view>
    </a-layout-content>
    </GlobalHeader>
  </a-layout>
</template>

<script setup lang="ts">
import { computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import GlobalHeader from '@/components/GlobalHeader.vue'
import AppChatPage from '@/pages/app/AppChatPage.vue'
import { useLoginUserStore } from '@/stores/loginUser'
import { useOpenedChatsStore } from '@/stores/openedChats'

const route = useRoute()
const router = useRouter()
const loginUserStore = useLoginUserStore()
const openedChatsStore = useOpenedChatsStore()

const isChatRoute = computed(() => route.name === '项目对话')

// 聊天路由统一入口：确保页签打开并激活（首页/编辑页/深链都汇聚到这里，幂等）。
// 注意必须监听 fullPath 而非 name：聊天页之间切换时 name 不变，监听 name 会导致页签无法切换
watch(
  () => route.fullPath,
  () => {
    if (route.name !== '项目对话') return
    const id = route.params.id as string | undefined
    if (!id) return
    if (!openedChatsStore.openChat(id)) {
      message.warning('最多同时打开3个项目，请先关闭一个页签')
      router.replace('/')
      return
    }
    openedChatsStore.setActive(id)
  },
  { immediate: true },
)

// 登出（手动/空闲超时）统一清空页签，避免换账号恢复脏页签、SSE 401 空跑
watch(
  () => loginUserStore.loginUser.id,
  (id) => {
    if (!id) openedChatsStore.clearAll()
  },
)
</script>

<style scoped>
.basic-layout {
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: var(--bg-page);
  position: relative;
  overflow: hidden;
}

.layout-bg-decor {
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 0;
  background:
    radial-gradient(ellipse 600px 400px at 90% 0%, rgba(59,130,246,0.04) 0%, transparent 70%),
    radial-gradient(ellipse 400px 500px at 5% 60%, rgba(99,102,241,0.03) 0%, transparent 70%),
    radial-gradient(ellipse 300px 300px at 70% 85%, rgba(59,130,246,0.03) 0%, transparent 70%);
}

.main-content {
  position: relative;
  z-index: 1;
  width: 100%;
  padding: 0;
  background: transparent;
  margin: 0;
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

/* 聊天工作区撑满内容区，内部实例各占满、靠 v-show 切换 */
.chat-workspace {
  height: 100%;
}

.chat-workspace-loading {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}
</style>
