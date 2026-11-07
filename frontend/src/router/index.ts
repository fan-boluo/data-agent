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
      name: '项目管理',
      component: () => import('@/pages/admin/AppManagePage.vue'),
    },
    {
      path: '/admin/monitor',
      name: '运行追踪',
      component: () => import('@/pages/admin/MonitorManagePage.vue'),
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
