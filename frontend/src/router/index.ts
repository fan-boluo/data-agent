import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      name: '我的项目',
      component: () => import('@/pages/HomePage.vue'),
    },
    {
      path: '/user/login',
      name: '用户登录',
      component: () => import('@/pages/user/UserLoginPage.vue'),
    },
    {
      path: '/user/register',
      name: '用户注册',
      component: () => import('@/pages/user/UserRegisterPage.vue'),
    },
    {
      path: '/admin/userManage',
      name: '用户管理',
      component: () => import('@/pages/admin/UserManagePage.vue'),
    },
    {
      path: '/admin/appManage',
      name: '项目信息',
      component: () => import('@/pages/admin/AppManagePage.vue'),
    },
    {
      path: '/admin/monitor',
      name: '运行追踪',
      component: () => import('@/pages/admin/MonitorManagePage.vue'),
    },
    {
      path: '/admin/toolStats',
      name: '运行统计',
      component: () => import('@/pages/admin/ToolStatsPage.vue'),
    },
    {
      path: '/admin/eval/agent',
      name: 'Agent运行评估',
      component: () => import('@/pages/admin/AgentRunEvalPage.vue'),
    },
    {
      path: '/admin/eval/dataset',
      name: '标准数据集评估',
      component: () => import('@/pages/admin/DatasetEvalPage.vue'),
    },
    {
      path: '/admin/platformResource',
      name: '平台资源',
      component: () => import('@/pages/admin/PlatformResourcePage.vue'),
    },
    {
      path: '/admin/platformConfig',
      name: '平台配置',
      component: () => import('@/pages/admin/PlatformConfigPage.vue'),
    },
    {
      // App owner 视角：先选项目，再在 Tool/Skill 两个 Tab 里开关授权
      path: '/app/grant',
      name: '项目授权',
      component: () => import('@/pages/app/AppGrantPage.vue'),
    },
    {
      // 裸 /app/chat（无项目 id）没有入口场景，统一回首页；带 id 的走下面的工作台路由
      path: '/app/chat',
      redirect: '/',
    },
    {
      path: '/app/chat/:id?',
      name: '项目对话',
      component: () => import('@/pages/app/AppChatPage.vue'),
    },
    {
      path: '/app/edit/:id',
      name: '编辑项目',
      component: () => import('@/pages/app/AppEditPage.vue'),
    },
  ],
})

export default router
