<template>
  <div class="app-chrome">
    <!-- 左侧边栏：logo + 竖排菜单（仅登录后可见），支持折叠 -->
    <aside v-if="loginUserStore.loginUser.id" class="app-sider" :class="{ collapsed }">
      <a class="sider-logo" href="/" @click.prevent="$router.push('/')">
        <BarChartOutlined class="logo-mark" />
        <span v-show="!collapsed" class="logo-text">数据分析平台</span>
      </a>
      <a-menu
        v-if="menuItems?.length"
        v-model:selectedKeys="selectedKeys"
        v-model:openKeys="openKeys"
        mode="inline"
        :inline-collapsed="collapsed"
        :items="menuItems"
        class="sider-menu"
        @click="handleMenuClick"
      />
      <a-button
        type="text"
        class="sider-collapse-btn"
        :title="collapsed ? '展开菜单' : '收起菜单'"
        @click="toggleCollapsed"
      >
        <MenuUnfoldOutlined v-if="collapsed" />
        <MenuFoldOutlined v-else />
        <span v-show="!collapsed" class="collapse-text">收起</span>
      </a-button>
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
  LineChartOutlined,
  ExperimentOutlined,
  ApiOutlined,
  BarChartOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
} from '@ant-design/icons-vue'

const loginUserStore = useLoginUserStore()
const router = useRouter()
const route = useRoute()

// 侧边栏折叠状态：本地持久化，刷新后保持
const COLLAPSE_KEY = 'codegenx:sider-collapsed'
const collapsed = ref(localStorage.getItem(COLLAPSE_KEY) === '1')
const toggleCollapsed = () => {
  collapsed.value = !collapsed.value
  localStorage.setItem(COLLAPSE_KEY, collapsed.value ? '1' : '0')
}

// 菜单选中态跟随路由（刷新/直接访问深链也能高亮）；进入子菜单页时自动展开父级
const selectedKeys = ref<string[]>([route.path])
const openKeys = ref<string[]>([])

const originItems: MenuProps['items'] = [
  {
    key: '/',
    icon: () => h(HomeOutlined),
    label: '我的项目',
    title: '我的项目',
  },
  {
    // 项目层：项目信息维护 + 项目资源授权
    key: '/admin/project',
    icon: () => h(AppstoreOutlined),
    label: '项目管理',
    title: '项目管理',
    children: [
      {
        key: '/admin/appManage',
        label: '项目信息',
        title: '项目信息',
      },
      {
        // 项目层能力授权：App owner 管理自己项目的 tool/skill 开关
        key: '/app/grant',
        label: '项目授权',
        title: '项目授权',
      },
    ],
  },
  {
    key: '/admin/userManage',
    icon: () => h(UserOutlined),
    label: '用户管理',
    title: '用户管理',
  },
  {
    // 平台层：仅管理员可见，平台资源总开关与平台级配置
    key: '/admin/platform',
    icon: () => h(ApiOutlined),
    label: '平台管理',
    title: '平台管理',
    children: [
      {
        key: '/admin/platformResource',
        label: '平台资源',
        title: '平台资源',
      },
      {
        key: '/admin/platformConfig',
        label: '平台配置',
        title: '平台配置',
      },
    ],
  },
  {
    key: '/admin/monitor',
    icon: () => h(DashboardOutlined),
    label: '运行追踪',
    title: '运行追踪',
  },
  {
    key: '/admin/toolStats',
    icon: () => h(LineChartOutlined),
    label: '运行统计',
    title: '运行统计',
  },
  {
    key: '/admin/eval',
    icon: () => h(ExperimentOutlined),
    label: '运行评估',
    title: '运行评估',
    children: [
      {
        key: '/admin/eval/agent',
        label: 'Agent运行评估',
        title: 'Agent运行评估',
      },
      {
        key: '/admin/eval/dataset',
        label: '标准数据集评估',
        title: '标准数据集评估',
      },
    ],
  },
]

// 子菜单项 → 父菜单 key 映射：路由变化时自动展开所属父级
const parentKeyOf: Record<string, string> = {}
const collectParentKeys = (items: MenuProps['items'], parentKey?: string) => {
  for (const item of items || []) {
    if (!item) continue
    const key = String((item as any).key || '')
    if (parentKey) parentKeyOf[key] = parentKey
    const children = (item as any).children
    if (Array.isArray(children)) collectParentKeys(children, key)
  }
}
collectParentKeys(originItems)

// 初始展开当前路由所属父级（刷新/深链直达子菜单页时）
const initialParentKey = parentKeyOf[route.path]
if (initialParentKey) openKeys.value = [initialParentKey]

watch(
  () => route.path,
  (path) => {
    selectedKeys.value = [path]
    const parentKey = parentKeyOf[path]
    if (parentKey && !openKeys.value.includes(parentKey)) {
      openKeys.value = [...openKeys.value, parentKey]
    }
  },
)

// 按叶子项做权限过滤：/admin 页面仅管理员可见；子项全被过滤时父菜单一并隐藏
const filterMenus = (items: MenuProps['items']): MenuProps['items'] => {
  const out: any[] = []
  for (const item of items || []) {
    if (!item) continue
    const key = 'key' in item ? String((item as any).key || '') : ''
    const children = (item as any).children
    if (Array.isArray(children)) {
      const filtered = filterMenus(children)
      if (filtered?.length) out.push({ ...item, children: filtered })
      continue
    }
    if (key.startsWith('/admin') && loginUserStore.loginUser.userRole !== 'admin') continue
    out.push(item)
  }
  return out
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
  width: 170px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: rgba(255, 255, 255, 0.72);
  backdrop-filter: blur(12px) saturate(1.4);
  -webkit-backdrop-filter: blur(12px) saturate(1.4);
  border-right: 1px solid var(--border-light);
  transition: width 0.2s var(--ease-out);
}

/* 折叠态：仅保留图标 */
.app-sider.collapsed {
  width: 64px;
}

.sider-logo {
  display: flex;
  align-items: center;
  gap: 8px;
  height: var(--header-height);
  padding: 0 16px;
  text-decoration: none;
  white-space: nowrap;
  flex-shrink: 0;
  overflow: hidden;
}

.app-sider.collapsed .sider-logo {
  padding: 0;
  justify-content: center;
}

.logo-mark {
  font-size: 20px;
  color: var(--accent-primary);
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
  padding: 8px;
  overflow-y: auto;
}

/* 竖排菜单项：胶囊选中态，收紧两侧留白 */
.sider-menu :deep(.ant-menu-item),
.sider-menu :deep(.ant-menu-submenu-title) {
  border-radius: var(--radius-btn);
  margin-block: 2px;
  margin-inline: 0;
  padding-inline: 10px;
  width: 100%;
  color: var(--text-secondary);
}

/* 子菜单加大左缩进，明确父子层级；缩小字号避免长文案被截断 */
.sider-menu :deep(.ant-menu-submenu .ant-menu-item) {
  padding-inline-start: 46px !important;
  padding-inline-end: 8px !important;
  font-size: 13px;
}

.sider-menu :deep(.ant-menu-submenu-arrow) {
  inset-inline-end: 10px;
}

.sider-menu :deep(.ant-menu-item-selected) {
  background: var(--accent-primary-light) !important;
  color: var(--accent-primary) !important;
  font-weight: 600;
}

.sider-menu :deep(.ant-menu-item::after) {
  display: none;
}

/* 折叠态：图标居中，收回自定义内边距 */
.app-sider.collapsed .sider-menu {
  padding: 8px 4px;
}

.app-sider.collapsed .sider-menu.ant-menu-inline-collapsed
  :deep(.ant-menu-item) {
  padding-inline: calc(50% - 8px) !important;
  text-align: center;
}

/* 底部折叠/展开按钮 */
.sider-collapse-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 40px;
  margin: 8px;
  flex-shrink: 0;
  color: var(--text-secondary);
  border-top: 1px solid var(--border-light);
  border-radius: 0;
}

.sider-collapse-btn:hover {
  color: var(--accent-primary);
  background: var(--accent-primary-light);
}

.app-sider.collapsed .sider-collapse-btn {
  margin: 8px 4px;
}

.collapse-text {
  font-size: 13px;
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
