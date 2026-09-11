<script setup>
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()
const isLogin = computed(() => route.path === '/login')
const username = ref(localStorage.getItem('username') || '')
// localStorage 非响应式：路由变化时同步用户名（登录/退出后不再显示旧用户）
watch(() => route.path, () => { username.value = localStorage.getItem('username') || '' })

function logout() {
  localStorage.removeItem('token')
  localStorage.removeItem('username')
  username.value = ''
  router.push('/login')
}
</script>

<template>
  <div v-if="!isLogin" class="topbar">
    <span class="logo">🐾 宠物健康管家</span>
    <nav>
      <router-link to="/home" :class="{ active: route.path === '/home' }">🏠 主页</router-link>
      <router-link to="/chat" :class="{ active: route.path === '/chat' }">🤖 AI 助手</router-link>
      <span class="user">👋 你好，{{ username }}</span>
      <button @click="logout">退出登录</button>
    </nav>
  </div>
  <router-view />
</template>
