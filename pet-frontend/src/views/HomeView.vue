<script setup>
import { ref, onMounted } from 'vue'
import api from '../api'

const pets = ref([])
const msg = ref({ type: '', text: '' })
const speciesOptions = ['猫咪', '狗狗', '其他']
const logTypes = ['进食', '饮水', '排泄', '睡眠', '活动', '其他']
const petForm = ref({ name: '', species: '猫咪', age: 0, breed: '', weight: 0 })
const logForm = ref({ pet_id: null, log_type: '进食', description: '', log_time: '' })

function showMsg(type, text) {
  msg.value = { type, text }
  setTimeout(() => (msg.value = { type: '', text: '' }), 3000)
}

async function loadPets() {
  try {
    const { data } = await api.get('/api/pets')
    pets.value = data
    // 首次加载或当前选中宠物已被删除时，默认选第一只，避免日志记到失效宠物
    if (data.length && (logForm.value.pet_id === null || !data.some(p => p.id === logForm.value.pet_id))) {
      logForm.value.pet_id = data[0].id
    }
  } catch (e) {
    showMsg('err', e.response?.data?.detail || '加载失败')
  }
}

async function addPet() {
  if (!petForm.value.name.trim()) return showMsg('err', '请添加宠物名字')
  try {
    const { data } = await api.post('/api/pets', petForm.value)
    showMsg('ok', data.message)
    petForm.value = { name: '', species: '猫咪', age: 0, breed: '', weight: 0 }
    loadPets()
  } catch (e) {
    showMsg('err', e.response?.data?.detail || '添加失败')
  }
}

async function delPet(id) {
  try {
    const { data } = await api.delete(`/api/pets/${id}`)
    showMsg('ok', data.message)
    loadPets()
  } catch (e) {
    showMsg('err', e.response?.data?.detail || '删除失败')
  }
}

async function addLog() {
  if (!logForm.value.pet_id) return showMsg('err', '请先添加宠物')
  try {
    const { data } = await api.post('/api/logs', logForm.value)
    showMsg('ok', data.message)
    logForm.value.description = ''
  } catch (e) {
    showMsg('err', e.response?.data?.detail || '保存失败')
  }
}

onMounted(loadPets)
</script>

<template>
  <div class="container">
    <div v-if="msg.text" class="msg" :class="msg.type">{{ msg.text }}</div>

    <div class="card">
      <h2>📝 添加宠物档案</h2>
      <div class="row">
        <div><label>宠物名字</label><input v-model="petForm.name" placeholder="请输入宠物名字" /></div>
        <div><label>种类</label><select v-model="petForm.species"><option v-for="s in speciesOptions" :key="s">{{ s }}</option></select></div>
        <div><label>品种（可选）</label><input v-model="petForm.breed" placeholder="比如布偶、金毛" /></div>
        <div><label>年龄</label><input v-model.number="petForm.age" type="number" min="0" step="0.1" /></div>
        <div><label>体重（kg）</label><input v-model.number="petForm.weight" type="number" min="0" step="0.1" /></div>
      </div>
      <button class="btn" style="margin-top:12px;" @click="addPet">添加宠物</button>
    </div>

    <div class="card">
      <h2>📊 记录宠物日常</h2>
      <div class="row">
        <div><label>选择宠物</label><select v-model="logForm.pet_id"><option v-for="p in pets" :key="p.id" :value="p.id">{{ p.name }}</option></select></div>
        <div><label>活动类型</label><select v-model="logForm.log_type"><option v-for="t in logTypes" :key="t">{{ t }}</option></select></div>
        <div><label>活动时间</label><input v-model="logForm.log_time" type="datetime-local" /></div>
        <div style="flex:2 1 300px;"><label>备注（可选）</label><input v-model="logForm.description" placeholder="如：吃了两碗猫粮、拉稀了、精神很好..." /></div>
      </div>
      <button class="btn" style="margin-top:12px;" @click="addLog">保存记录</button>
    </div>

    <div class="card">
      <h2>📋 我的宠物</h2>
      <p v-if="!pets.length" style="color:#6b7280;">您尚未添加宠物</p>
      <div v-for="p in pets" :key="p.id" class="pet-item">
        <div>
          <b>{{ p.name }}</b>，{{ p.species }}
          <div style="font-size:12px;color:#6b7280;">
            品种：{{ p.breed || '未填写' }} | 年龄：{{ p.age }}岁 | 体重：{{ p.weight }}kg | 添加于 {{ p.created_at.slice(0, 10) }}
          </div>
        </div>
        <button class="btn danger" @click="delPet(p.id)">🗑️ 删除</button>
      </div>
    </div>
  </div>
</template>
