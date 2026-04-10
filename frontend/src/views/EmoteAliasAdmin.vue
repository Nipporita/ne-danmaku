<script setup>
import { ref, computed, onMounted } from 'vue'

/* ---------- auth ---------- */
const params = new URLSearchParams(window.location.search)
const key = params.get('key') || ''
const BASE = '/api/danmaku/v1/emote-alias'

function api(path, opts = {}) {
  const sep = path.includes('?') ? '&' : '?'
  return fetch(`${BASE}${path}${sep}key=${encodeURIComponent(key)}`, opts)
}

/* ---------- state ---------- */
const emoteNames = ref([])   // all scanned emote names
const aliases = ref([])       // existing alias records
const search = ref('')
const loading = ref(false)
const error = ref('')

// map: emote_name -> loaded image URL (lazy)
const loadedImages = ref({})

// new-alias form
const newOriginal = ref('')
const newAlias = ref('')

/* ---------- filtered ---------- */
const filteredEmotes = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return emoteNames.value
  return emoteNames.value.filter(n => n.toLowerCase().includes(q))
})

function aliasesFor(name) {
  return aliases.value.filter(a => a.original_name === name)
}

/* ---------- fetch ---------- */
async function loadData() {
  loading.value = true
  error.value = ''
  try {
    const [eRes, aRes] = await Promise.all([
      api('/emotes'),
      api('/aliases'),
    ])
    if (!eRes.ok) throw new Error((await eRes.json().catch(() => ({}))).detail || `${eRes.status}`)
    if (!aRes.ok) throw new Error((await aRes.json().catch(() => ({}))).detail || `${aRes.status}`)
    emoteNames.value = await eRes.json()
    aliases.value = await aRes.json()
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

/* ---------- lazy image ---------- */
async function loadImage(name) {
  if (loadedImages.value[name]) return
  try {
    const res = await api(`/emote-image/${encodeURIComponent(name)}`)
    if (!res.ok) return
    const data = await res.json()
    loadedImages.value[name] = data.url
  } catch { /* ignore */ }
}

/* ---------- alias CRUD ---------- */
async function addAlias(originalName, aliasText) {
  if (!aliasText || !aliasText.trim()) return
  error.value = ''
  try {
    const res = await api('/aliases', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ original_name: originalName, alias: aliasText.trim() }),
    })
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `${res.status}`)
    const created = await res.json()
    aliases.value.push(created)
  } catch (e) {
    error.value = e.message
  }
}

async function removeAlias(id) {
  error.value = ''
  try {
    const res = await api(`/aliases/${id}`, { method: 'DELETE' })
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `${res.status}`)
    aliases.value = aliases.value.filter(a => a.id !== id)
  } catch (e) {
    error.value = e.message
  }
}

function submitInline(name) {
  const input = inlineInputs.value[name]
  if (input && input.trim()) {
    addAlias(name, input)
    inlineInputs.value[name] = ''
  }
}

const inlineInputs = ref({})

onMounted(loadData)
</script>

<template>
  <div class="alias-page">
    <div class="alias-container">
      <h1 class="title">表情别名管理</h1>
      <p class="subtitle">为已有表情设置别名，别名可重复</p>

      <div v-if="error" class="error-box">{{ error }}</div>

      <div class="search-bar">
        <input
          v-model="search"
          type="text"
          placeholder="搜索表情名…"
          class="search-input"
        >
      </div>

      <div v-if="loading" class="empty-hint">加载中…</div>

      <div v-else-if="filteredEmotes.length === 0" class="empty-hint">
        {{ emoteNames.length === 0 ? '没有可用的表情' : '没有匹配的表情' }}
      </div>

      <div v-else class="emote-list">
        <div v-for="name in filteredEmotes" :key="name" class="emote-card">
          <div class="emote-header">
            <span class="emote-name">{{ name }}</span>
            <button
              v-if="!loadedImages[name]"
              class="btn-small btn-load"
              @click="loadImage(name)"
            >
              加载图片
            </button>
            <img
              v-else
              :src="loadedImages[name]"
              class="emote-preview"
              :alt="name"
            >
          </div>

          <!-- existing aliases -->
          <div v-if="aliasesFor(name).length" class="alias-tags">
            <span v-for="a in aliasesFor(name)" :key="a.id" class="alias-tag">
              {{ a.alias }}
              <button class="tag-del" @click="removeAlias(a.id)">&times;</button>
            </span>
          </div>

          <!-- inline add -->
          <div class="inline-add">
            <input
              v-model="inlineInputs[name]"
              type="text"
              placeholder="添加别名…"
              class="inline-input"
              @keyup.enter="submitInline(name)"
            >
            <button class="btn-small btn-add" @click="submitInline(name)">+</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.alias-page {
  min-height: 100vh;
  background: linear-gradient(135deg, #f0f4ff 0%, #e8eeff 100%);
  padding: 2rem 1rem;
}
.alias-container {
  max-width: 640px;
  margin: 0 auto;
}
.title {
  font-size: 1.8rem;
  font-weight: 700;
  text-align: center;
  color: #333;
  margin-bottom: 0.4rem;
}
.subtitle {
  text-align: center;
  color: #999;
  margin-bottom: 1.2rem;
  font-size: 0.95rem;
}
.error-box {
  padding: 0.8rem 1rem;
  background: #fef2f2;
  border: 1px solid #fecaca;
  border-radius: 8px;
  color: #dc2626;
  font-size: 0.9rem;
  margin-bottom: 1rem;
}
.search-bar {
  margin-bottom: 1.2rem;
}
.search-input {
  width: 100%;
  padding: 0.6rem 1rem;
  border: 1px solid #ddd;
  border-radius: 8px;
  font-size: 1rem;
  outline: none;
  box-sizing: border-box;
  transition: border-color 0.2s, box-shadow 0.2s;
}
.search-input:focus {
  border-color: #6366f1;
  box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.15);
}
.empty-hint {
  text-align: center;
  color: #bbb;
  padding: 3rem 0;
}

/* emote cards */
.emote-list {
  display: flex;
  flex-direction: column;
  gap: 0.8rem;
}
.emote-card {
  background: #fff;
  border-radius: 10px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.06);
  padding: 1rem 1.2rem;
  border: 1px solid #f0f0f0;
}
.emote-header {
  display: flex;
  align-items: center;
  gap: 0.8rem;
  margin-bottom: 0.5rem;
}
.emote-name {
  font-weight: 600;
  font-size: 1rem;
  color: #333;
  flex: 1;
  word-break: break-all;
}
.emote-preview {
  width: 40px;
  height: 40px;
  object-fit: contain;
  border-radius: 4px;
  background: #f9f9f9;
}

/* tags */
.alias-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  margin-bottom: 0.5rem;
}
.alias-tag {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  background: #eef2ff;
  color: #4f46e5;
  padding: 0.2rem 0.6rem;
  border-radius: 6px;
  font-size: 0.85rem;
}
.tag-del {
  background: none;
  border: none;
  color: #a5b4fc;
  cursor: pointer;
  font-size: 1rem;
  line-height: 1;
  padding: 0 0.1rem;
}
.tag-del:hover {
  color: #ef4444;
}

/* inline add */
.inline-add {
  display: flex;
  gap: 0.4rem;
}
.inline-input {
  flex: 1;
  padding: 0.35rem 0.6rem;
  border: 1px solid #e0e0e0;
  border-radius: 6px;
  font-size: 0.9rem;
  outline: none;
}
.inline-input:focus {
  border-color: #6366f1;
}

/* buttons */
.btn-small {
  padding: 0.3rem 0.7rem;
  border: none;
  border-radius: 6px;
  font-size: 0.85rem;
  cursor: pointer;
  transition: background 0.2s;
}
.btn-load {
  background: #e0e7ff;
  color: #4338ca;
}
.btn-load:hover {
  background: #c7d2fe;
}
.btn-add {
  background: #6366f1;
  color: #fff;
  font-weight: 600;
  font-size: 1rem;
  padding: 0.3rem 0.8rem;
}
.btn-add:hover {
  background: #4f46e5;
}
</style>
