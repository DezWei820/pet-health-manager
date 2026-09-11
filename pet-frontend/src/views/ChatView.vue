<script setup>
import { ref, onMounted, nextTick } from 'vue'
import { marked } from 'marked'
import api, { streamChat } from '../api'

// Markdown 渲染：先转义 < > 防 XSS，再渲染
// ① 表格块（表头+分隔行）前补空行（CommonMark 要求表格前有空行，否则管道符裸露）
// ② 删掉 *** 等非标准强调（DeepSeek 常输出残缺 ***，会被 marked 解释成大块斜体加粗）
// ③ **文字**中文 → 补空格（CommonMark 要求闭合 ** 后不能紧跟文字）
// ④ 渲染后清理所有残留的孤立 *，保证不裸露任何星号
const renderMd = (s) => {
  let t = String(s || '').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  t = t.replace(/([^\n])\n(\|[^\n]*\|\n\|[-: |]+\|)/g, '$1\n\n$2')
  t = t.replace(/\*{3,}/g, '')
  t = t.replace(/\*\*([^*\n]+)\*\*(?=[\u4e00-\u9fa5])/g, '**$1** ')
  return marked.parse(t).replace(/\*+/g, '')
}

const convs = ref([])
const convId = ref(null)
const msgs = ref([])
const input = ref('')
const pets = ref([])
const petId = ref(0)
const typing = ref(false)
const err = ref('')
const ocrText = ref('')
const msgsBox = ref(null)

function scrollDown() {
  nextTick(() => {
    if (msgsBox.value) msgsBox.value.scrollTop = msgsBox.value.scrollHeight
  })
}

async function loadMsgs() {
  const { data } = await api.get(`/api/conversations/${convId.value}/messages`)
  msgs.value = data
  scrollDown()
}

async function newConv() {
  const { data } = await api.post('/api/conversations')
  convs.value.unshift(data)
  convId.value = data.id
  msgs.value = []
}

async function loadConvs() {
  const { data } = await api.get('/api/conversations')
  convs.value = data
  if (!convs.value.length) return newConv()
  if (!convs.value.some((c) => c.id === convId.value)) {
    convId.value = convs.value[0].id
    await loadMsgs()
  }
}

async function switchConv(id) {
  convId.value = id
  await loadMsgs()
}

async function delConv(id) {
  await api.delete(`/api/conversations/${id}`)
  convs.value = convs.value.filter((c) => c.id !== id)
  if (convId.value === id) {
    convId.value = null
    msgs.value = []
  }
  if (!convs.value.length) newConv()
}

async function send() {
  const text = input.value.trim()
  if (!text || typing.value) return
  input.value = ''
  err.value = ''
  msgs.value.push({ role: 'user', content: text })
  msgs.value.push({ role: 'assistant', content: '' })
  typing.value = true
  scrollDown()
  const payload = { message: text, conversation_id: convId.value }
  if (petId.value) payload.pet_id = petId.value
  await streamChat(
    payload,
    (t) => {
      msgs.value[msgs.value.length - 1].content += t
      scrollDown()
    },
    (e) => { err.value = e }
  )
  typing.value = false
}

async function ocrUpload(e) {
  const file = e.target.files[0]
  if (!file) return
  const fd = new FormData()
  fd.append('file', file)
  try {
    const { data } = await api.post('/api/ocr', fd, {
      headers: { 'Content-Type': 'multipart/form-data' }
    })
    ocrText.value = data.text || data.error || '识别失败'
  } catch {
    ocrText.value = '识别失败'
  }
  e.target.value = ''
}

function sendOcr() {
  if (!ocrText.value) return
  input.value = ocrText.value
  ocrText.value = ''
  send()
}

async function loadPets() {
  try {
    const { data } = await api.get('/api/pets')
    pets.value = data
  } catch { /* 忽略 */ }
}

onMounted(async () => {
  await Promise.all([loadConvs(), loadPets()])
})
</script>

<template>
  <div class="container chat-wrap">
    <aside class="sidebar">
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
          <b>📚 会话</b>
          <button class="btn secondary" @click="newConv">➕ 新建</button>
        </div>
        <div v-if="!convs.length" style="color:#6b7280;font-size:13px;padding:8px 0;">暂无会话</div>
        <div
          v-for="c in convs" :key="c.id"
          class="conv-item" :class="{ active: c.id === convId }"
          @click="switchConv(c.id)"
        >
          <span class="ellipsis">{{ c.title }}</span>
          <button class="del" title="删除该会话" @click.stop="delConv(c.id)">🗑️</button>
        </div>
      </div>
    </aside>

    <div class="chat-main">
      <div class="card" style="display:flex;gap:16px;align-items:center;flex-wrap:wrap;">
        <b>🤖 AI 健康助手</b>
        <select v-model="petId" style="width:auto;">
          <option :value="0">不指定宠物</option>
          <option v-for="p in pets" :key="p.id" :value="p.id">{{ p.name }}</option>
        </select>
        <label style="margin:0;font-size:13px;">📷 图片识别：</label>
        <input type="file" accept="image/*" style="width:auto;" @change="ocrUpload" />
      </div>

      <div v-if="ocrText" class="card" style="background:#f0f9eb;">
        <div style="font-size:13px;color:#389e0d;margin-bottom:6px;">📷 OCR 识别结果</div>
        <pre style="white-space:pre-wrap;font-size:13px;margin-bottom:8px;">{{ ocrText }}</pre>
        <button class="btn" @click="sendOcr">发送给 AI 分析</button>
      </div>

      <div ref="msgsBox" class="msgs">
        <div v-if="!msgs.length" style="color:#6b7280;text-align:center;padding:40px 0;">
          开始和 AI 助手聊聊吧～
        </div>
        <div v-for="(m, i) in msgs" :key="i" class="bubble" :class="m.role === 'user' ? 'user' : 'ai'">
          <!-- 流式进行中：纯文本显示（保留换行），避免半截 Markdown 渲染成一团；结束后再渲染 -->
          <div v-if="typing && i === msgs.length - 1" style="white-space:pre-wrap;word-break:break-word;">{{ m.content }}<span class="cursor">▌</span></div>
          <div v-else v-html="renderMd(m.content)"></div>
        </div>
        <div v-if="err" class="msg err">{{ err }}</div>
      </div>

      <div class="input-row">
        <input
          v-model="input" :disabled="typing"
          placeholder="请输入您的问题，例如：我的猫咪最近食欲不振怎么办？"
          @keyup.enter="send"
        />
        <button class="btn" :disabled="typing" @click="send">发送</button>
      </div>
    </div>
  </div>
</template>
