import { createRouter, createWebHistory } from 'vue-router'
import LoginView from './views/LoginView.vue'
import HomeView from './views/HomeView.vue'
import ChatView from './views/ChatView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/home' },
    { path: '/login', component: LoginView },
    { path: '/home', component: HomeView, meta: { auth: true } },
    { path: '/chat', component: ChatView, meta: { auth: true } }
  ]
})

// 登录守卫：未登录跳登录页，已登录访问登录页跳主页
router.beforeEach((to) => {
  const token = localStorage.getItem('token')
  if (to.meta.auth && !token) return '/login'
  if (to.path === '/login' && token) return '/home'
})

export default router
