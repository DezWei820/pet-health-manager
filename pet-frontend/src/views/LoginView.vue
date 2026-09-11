<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import api from '../api'

const router = useRouter()
const tab = ref('login')
const form = ref({ username: '', password: '', confirm: '' })
const msg = ref('')

async function submit() {
  msg.value = ''
  if (!form.value.username || !form.value.password) {
    msg.value = '请填写用户名和密码'
    return
  }
  try {
    if (tab.value === 'login') {
      const { data } = await api.post('/api/login', {
        username: form.value.username, password: form.value.password
      })
      localStorage.setItem('token', data.access_token)
      localStorage.setItem('username', form.value.username)
      router.push('/home')
    } else {
      if (form.value.password !== form.value.confirm) { msg.value = '两次密码不一致'; return }
      if (form.value.password.length < 6) { msg.value = '密码长度至少6位'; return }
      const { data } = await api.post('/api/register', {
        username: form.value.username, password: form.value.password
      })
      msg.value = data.message + '，请登录'
      tab.value = 'login'
      form.value.password = ''
      form.value.confirm = ''
    }
  } catch (e) {
    msg.value = e.response?.data?.detail || e.message || '操作失败'
  }
}
</script>

<template>
  <div class="login-wrap">
    <div class="card login-card">
      <h1 style="text-align:center;font-size:22px;margin-bottom:4px;">🐾 宠物健康管家</h1>
      <p style="text-align:center;color:#6b7280;font-size:13px;margin-bottom:16px;">你的宠物健康助手</p>
      <div style="display:flex;gap:8px;margin-bottom:16px;">
        <button class="btn" :class="tab === 'login' ? '' : 'secondary'" style="flex:1;" @click="tab='login'">登录</button>
        <button class="btn" :class="tab === 'register' ? '' : 'secondary'" style="flex:1;" @click="tab='register'">注册</button>
      </div>
      <label>用户名</label>
      <input v-model="form.username" placeholder="请输入用户名" />
      <label>密码</label>
      <input v-model="form.password" type="password" placeholder="请输入密码" @keyup.enter="submit" />
      <template v-if="tab === 'register'">
        <label>确认密码</label>
        <input v-model="form.confirm" type="password" placeholder="请再次输入密码" @keyup.enter="submit" />
      </template>
      <div v-if="msg" class="msg err">{{ msg }}</div>
      <button class="btn" style="width:100%;margin-top:14px;" @click="submit">
        {{ tab === 'login' ? '登录' : '注册' }}
      </button>
    </div>
  </div>
</template>
