<template>
  <div class="app-chrome">
    <!-- 左侧边栏：logo + 竖排菜单（仅登录后可见） -->
    <aside v-if="loginUserStore.loginUser.id" class="app-sider">
      <a class="sider-logo" href="/" @click.prevent="$router.push('/')">
        <span class="logo-text">数据分析平台</span>
      </a>
      <a-menu
        v-if="menuItems?.length"
        v-model:selectedKeys="selectedKeys"
        mode="inline"
        :items="menuItems"
        class="sider-menu"
        @click="handleMenuClick"
      />
    </aside>

    <div class="app-main">
      <!-- 顶部栏：打开的项目页签 + 用户区；未登录时仅展示 logo -->
      <header class="app-topbar">
        <a
          v-if="!loginUserStore.loginUser.id"
          class="sider-logo"
          href="/"
          @click.prevent="$router.push('/')"
        >
          <span class="logo-text">数据分析平台</span>
        </a>
        <ChatTabsBar />
        <div v-if="loginUserStore.loginUser.id" class="header-right">
          <div class="user-info">
            <a-dropdown>
              <div class="user-dropdown-trigger">
                <a-avatar :size="28">
                  {{ loginUserStore.loginUser.userName?.charAt(0) || 'U' }}
                </a-avatar>
                <span class="user-name">{{ loginUserStore.loginUser.userName }}</span>
              </div>
              <template #overlay>
                <a-menu @click="doLogout">
                  <a-menu-item key="logout">
                    <LogoutOutlined />
                    <span>退出登录</span>
                  </a-menu-item>
                </a-menu>
              </template>
            </a-dropdown>
          </div>
        </div>
      </header>

      <!-- 页面内容由 BasicLayout 经 slot 注入 -->
      <div class="app-body">
        <slot />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, h, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { type MenuProps, message } from 'ant-design-vue'
import { useLoginUserStore } from '@/stores/loginUser.ts'
import { userLogout } from '@/api/userController.ts'
import { clearLocalAuth } from '@/request'
import { useIdleTimeout } from '@/composables/useIdleTimeout'
import ChatTabsBar from '@/components/ChatTabsBar.vue'
import {
  LogoutOutlined,
  HomeOutlined,
  AppstoreOutlined,
  UserOutlined,
  DashboardOutlined,
} from '@ant-design/icons-vue'

const loginUserStore = useLoginUserStore()
const router = useRouter()
const route = useRoute()

// 菜单选中态跟随路由（刷新/直接访问深链也能高亮）
const selectedKeys = ref<string[]>([route.path])
watch(
  () => route.path,
  (path) => {
    selectedKeys.value = [path]
  },
)

const originItems: MenuProps['items'] = [
  {
    key: '/',
    icon: () => h(HomeOutlined),
    label: '我的项目',
    title: '我的项目',
  },
  {
    key: '/admin/appManage',
    icon: () => h(AppstoreOutlined),
    label: '项目管理',
    title: '项目管理',
  },
  {
    key: '/admin/userManage',
    icon: () => h(UserOutlined),
    label: '用户管理',
    title: '用户管理',
  },
  {
    key: '/admin/monitor',
    icon: () => h(DashboardOutlined),
    label: '运行追踪',
    title: '运行追踪',
  },
]

const filterMenus = (items: MenuProps['items']): MenuProps['items'] => {
  if (!items) return []
  return items.filter((item) => {
    if (!item) return false
    const key = 'key' in item ? (item as any).key : ''
    if (key.startsWith('/admin')) {
      return loginUserStore.loginUser.userRole === 'admin'
    }
    return true
  })
}

const menuItems = computed<MenuProps['items']>(() => filterMenus(originItems))

const handleMenuClick: MenuProps['onClick'] = (e) => {
  const key = e.key as string
  selectedKeys.value = [key]
  if (key.startsWith('/')) {
    router.push(key)
  }
}

const performIdleLogout = () => {
  if (!loginUserStore.loginUser.id) return
  // 空闲超时登出也通知后端 revoke token
  userLogout().catch(() => {})
  clearLocalAuth()
  loginUserStore.setLoginUser({ userName: '未登录' })
  message.warning('长时间未操作，已自动退出登录')
  router.push('/user/login')
}

useIdleTimeout(performIdleLogout)

const doLogout = async () => {
  const res = await userLogout()
  if (res.data.code === 0) {
    clearLocalAuth()
    loginUserStore.setLoginUser({ userName: '未登录' })
    message.success('退出登录成功')
    await router.push('/user/login')
  } else {
    message.error('退出登录失败，' + res.data.message)
  }
}
</script>

<style scoped>
/* 整体框架：左侧边栏 + 右侧（顶部栏 + 内容） */
.app-chrome {
  display: flex;
  flex: 1;
  min-height: 0;
}

/* ---- 左侧边栏 ---- */
.app-sider {
  width: 160px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: rgba(255, 255, 255, 0.72);
  backdrop-filter: blur(12px) saturate(1.4);
  -webkit-backdrop-filter: blur(12px) saturate(1.4);
  border-right: 1px solid var(--border-light);
}

.sider-logo {
  display: flex;
  align-items: center;
  height: var(--header-height);
  padding: 0 20px;
  text-decoration: none;
  white-space: nowrap;
  flex-shrink: 0;
}

.logo-text {
  font-size: 18px;
  font-weight: 700;
  letter-spacing: 0.02em;
  /* 蓝→靛渐变文字，与主按钮/头像同一科技色系 */
  background: linear-gradient(135deg, var(--accent-primary), #6366f1);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
}

.sider-menu {
  flex: 1;
  border-inline-end: none !important;
  background: transparent !important;
  padding: 8px 10px;
  overflow-y: auto;
}

/* 竖排菜单项：胶囊选中态，收紧两侧留白 */
.sider-menu :deep(.ant-menu-item) {
  border-radius: var(--radius-btn);
  margin-block: 2px;
  margin-inline: 0;
  padding-inline: 12px;
  width: 100%;
  color: var(--text-secondary);
}

.sider-menu :deep(.ant-menu-item-selected) {
  background: var(--accent-primary-light) !important;
  color: var(--accent-primary) !important;
  font-weight: 600;
}

.sider-menu :deep(.ant-menu-item::after) {
  display: none;
}

/* ---- 右侧主区 ---- */
.app-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.app-topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: var(--header-height);
  padding: 0 24px;
  flex-shrink: 0;
  background: rgba(255, 255, 255, 0.72);
  backdrop-filter: blur(12px) saturate(1.4);
  -webkit-backdrop-filter: blur(12px) saturate(1.4);
  border-bottom: 1px solid var(--border-light);
}

.app-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.header-right {
  display: flex;
  align-items: center;
  flex-shrink: 0;
  /* 无项目页签时也要把用户区顶到最右侧 */
  margin-left: auto;
}

.user-info {
  display: flex;
  align-items: center;
  cursor: pointer;
}

.user-dropdown-trigger {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 10px;
  border-radius: var(--radius-btn);
  transition: background 0.18s ease;
}

.user-dropdown-trigger:hover {
  background: var(--accent-primary-light);
}

.user-name {
  margin-left: 4px;
  font-size: 14px;
  font-weight: 500;
  color: var(--text-primary);
}
</style>
