import axios from 'axios'

// 普通请求：axios 实例，自动带 JWT
const api = axios.create({ baseURL: '/', timeout: 20000 })

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

// 统一响应处理：401 清登录态回登录页；网络错误给明确提示
api.interceptors.response.use(
  (resp) => resp,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem('token')
      localStorage.removeItem('username')
      window.location.href = '/login'
    } else if (!error.response) {
      error.message = '无法连接服务器，请确认后端已启动'
    }
    return Promise.reject(error)
  }
)

// 流式对话：后端是 POST + JWT 的 SSE，EventSource 只支持 GET，必须用 fetch 读流
export async function streamChat(data, onToken, onError) {
  try {
    const resp = await fetch('/api/ai/chat/stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${localStorage.getItem('token')}`
      },
      body: JSON.stringify(data)
    })
    if (!resp.ok || !resp.body) throw new Error('AI服务暂时不可用')
    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let event = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let idx
      while ((idx = buffer.indexOf('\n')) !== -1) {
        const line = buffer.slice(0, idx).replace(/\r$/, '')
        buffer = buffer.slice(idx + 1)
        if (!line) continue
        if (line.startsWith('event:')) {
          event = line.slice(6).trim()
        } else if (line.startsWith('data:')) {
          // 只去 SSE 约定的前导空格，保留内容（含尾部空白）；后端把 \n 转义为字面 \n，这里还原
          const payload = line.slice(5).replace(/^ /, '')
          if (event === 'token') onToken(payload.replace(/\\n/g, '\n'))
          else if (event === 'error') onError(payload)
        }
      }
    }
  } catch (e) {
    onError(e.message || '无法连接服务器')
  }
}

export default api
