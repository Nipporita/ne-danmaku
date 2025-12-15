<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'

const props = defineProps({
  roomId: {
    type: String,
    required: true,
  },
  authKey: {
    type: String,
    default: '',
  },
})

const containerRef = ref(null)
const messages = ref([])
const loading = ref(true)
const inputValue = ref('')
const senderName = ref('元火子')
const error = ref('')
const clientSocket = ref(null)
const clientSocketOk = ref(false)
const upstreamSocket = ref(null)
const upstreamSocketOk = ref(false)
const reconnectAttempts = ref(0)
const authToken = ref('')
const opacityValue = ref(100)

const MESSAGE_LIMIT = 100
const MAX_MESSAGE_LENGTH = 50

const canSend = computed(() => {
  return upstreamSocket.value
    && clientSocketOk.value
    && inputValue.value.trim()
    && senderName.value.trim()
    && authToken.value.trim()
    && inputValue.value.length <= MAX_MESSAGE_LENGTH
})

const hasAuthKey = computed(() => Boolean(authToken.value))
const canSendOpacity = computed(() => upstreamSocketOk.value && hasAuthKey.value)

function showMessage(msg) {
  messages.value.push(msg)
  if (messages.value.length > MESSAGE_LIMIT)
    messages.value.shift()

  nextTick(() => {
    if (containerRef.value)
      containerRef.value.scrollTop = containerRef.value.scrollHeight
  })
}

function sendMessage() {
  if (inputValue.value.length > MAX_MESSAGE_LENGTH) {
    error.value = `弹幕长度不能超过${MAX_MESSAGE_LENGTH}字符`
    return
  }

  if (!canSend.value)
    return

  const packet = {
    group: props.roomId,
    danmaku: {
      text: inputValue.value.trim(),
      sender: senderName.value.trim(),
    },
  }
  upstreamSocket.value.send(JSON.stringify(packet))

  inputValue.value = ''
  error.value = ''
}

function sendOpacityCommand() {
  if (!canSendOpacity.value || !upstreamSocket.value)
    return

  const normalized = Math.max(0, Math.min(100, Number(opacityValue.value) || 0))
  const packet = {
    group: props.roomId,
    control: {
      type: 'setOpacity',
      value: normalized,
    },
  }

  upstreamSocket.value.send(JSON.stringify(packet))
  showMessage({ text: `已设置全局透明度为 ${normalized}%`, source: 'system' })
}

function connectClientWebSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss://' : 'ws://'
  const wsUrl = `${protocol}${window.location.host}/api/danmaku/v1/danmaku/${props.roomId}`

  if (clientSocketOk.value)
    return

  if (clientSocket.value)
    clientSocket.value.close()

  clientSocket.value = new WebSocket(wsUrl)

  clientSocket.value.onopen = () => {
    checkConnectionStatus()
  }

  clientSocket.value.onmessage = (event) => {
    const data = JSON.parse(event.data)
    showMessage({ ...data, source: 'client' })
  }

  clientSocket.value.onclose = () => {
    checkConnectionStatus()
    const reconnectDelay = Math.min(30000, 2 ** reconnectAttempts.value * 1000)
    setTimeout(connectClientWebSocket, reconnectDelay)
    reconnectAttempts.value++
  }

  clientSocket.value.onerror = () => {
    clientSocket.value?.close()
  }
}

function connectUpstreamWebSocket() {
  if (!authToken.value)
    return

  if (upstreamSocketOk.value)
    return

  const protocol = window.location.protocol === 'https:' ? 'wss://' : 'ws://'
  const wsUrl = `${protocol}${window.location.host}/api/danmaku/v1/upstream?token=${authToken.value}`

  if (upstreamSocket.value)
    upstreamSocket.value.close()

  upstreamSocket.value = new WebSocket(wsUrl)

  upstreamSocket.value.onopen = () => {
    checkConnectionStatus()
  }

  upstreamSocket.value.onmessage = (event) => {
    const data = JSON.parse(event.data)

    if (data.error) {
      showMessage({ text: `上游错误: ${data.error}`, source: 'upstream' })
      return
    }

    showMessage({ ...data, source: 'upstream' })
  }

  upstreamSocket.value.onclose = () => {
    checkConnectionStatus()
    if (authToken.value) {
      const reconnectDelay = Math.min(30000, 2 ** reconnectAttempts.value * 1000)
      setTimeout(connectUpstreamWebSocket, reconnectDelay)
      reconnectAttempts.value++
    }
  }

  upstreamSocket.value.onerror = () => {
    showMessage({ text: '上游连接失败，请检查Token是否正确', source: 'upstream' })
  }
}

function checkConnectionStatus() {
  const clientConnected = clientSocket.value && clientSocket.value.readyState === WebSocket.OPEN
  clientSocketOk.value = Boolean(clientConnected)
  const upstreamConnected = upstreamSocket.value && upstreamSocket.value.readyState === WebSocket.OPEN
  upstreamSocketOk.value = Boolean(upstreamConnected)

  if (clientConnected || upstreamConnected) {
    if (loading.value) {
      loading.value = false
      reconnectAttempts.value = 0

      const connections = []
      if (clientConnected)
        connections.push('客户端')
      if (upstreamConnected)
        connections.push('上游')

      showMessage({
        text: `元火弹幕姬已连接~ (${connections.join('、')})`,
        source: 'system',
      })
    }
  }
  else {
    loading.value = true
    showMessage({
      text: '元火弹幕姬已断开~',
      source: 'system',
    })
  }
}

function connectWebSocket() {
  loading.value = true
  connectClientWebSocket()
  connectUpstreamWebSocket()
}

watch(inputValue, () => {
  if (error.value)
    error.value = ''
})

watch(() => props.authKey, (value) => {
  const normalized = typeof value === 'string' ? value.trim() : ''
  if (normalized !== authToken.value)
    authToken.value = normalized
}, { immediate: true })

watch(authToken, () => {
  if (authToken.value) {
    connectUpstreamWebSocket()
  }
  else if (upstreamSocket.value) {
    upstreamSocket.value.close()
  }
})

onMounted(() => {
  connectWebSocket()
})

onUnmounted(() => {
  clientSocket.value?.close()
  upstreamSocket.value?.close()
})
</script>

<template>
  <div class="chat-shell">
    <header class="chat-status">
      <div class="status-pill" :class="{ connected: clientSocketOk }">
        <span class="status-dot" :class="{ connected: clientSocketOk }" />
        <span>客户端连接</span>
      </div>
      <div class="status-pill" :class="{ connected: upstreamSocketOk }">
        <span class="status-dot" :class="{ connected: upstreamSocketOk }" />
        <span>上游连接</span>
      </div>
      <span v-if="loading" class="status-note">正在重连...</span>
    </header>

    <section ref="containerRef" class="chat-messages">
      <article v-for="(message, index) in messages" :key="index" class="message-card">
        <header class="message-meta">
          <strong v-if="message.sender" class="message-sender">{{ message.sender }}</strong>
          <span v-if="message.source" class="message-tag" :class="message.source">
            {{ message.source === 'client' ? '客户端'
              : message.source === 'upstream' ? '上游' : '系统' }}
          </span>
        </header>
        <p class="message-text">{{ message.text }}</p>
      </article>
    </section>

    <section v-if="hasAuthKey" class="opacity-panel">
      <div class="panel-title">全局弹幕控制</div>
      <label class="slider-label">
        <span>弹幕透明度</span>
        <input v-model.number="opacityValue" type="range" min="0" max="100" />
        <span class="slider-value">{{ opacityValue }}%</span>
      </label>
      <button class="primary-btn" :disabled="!canSendOpacity" @click="sendOpacityCommand">
        应用指令
      </button>
    </section>

    <section class="composer">
      <div class="auth-hint" :class="{ ready: hasAuthKey }">
        {{ hasAuthKey ? 'URL key 已加载' : '缺少 URL key，无法连接上游' }}
      </div>
      <input v-model="senderName" class="text-input" type="text" placeholder="输入昵称..." />
      <input
        v-model="inputValue"
        class="text-input"
        type="text"
        :maxlength="MAX_MESSAGE_LENGTH"
        placeholder="输入弹幕..."
        @keydown.enter="sendMessage"
      />
      <button class="primary-btn" :disabled="!canSend" @click="sendMessage">
        发送数据包
      </button>
      <div v-if="error" class="error-text">{{ error }}</div>
    </section>

    <div v-if="inputValue" class="char-counter">
      {{ inputValue.length }}/{{ MAX_MESSAGE_LENGTH }}
    </div>
  </div>
</template>

<style scoped>
.chat-shell {
  height: 100vh;
  width: 100vw;
  display: flex;
  flex-direction: column;
  background: rgba(2, 6, 23, 0.75);
  backdrop-filter: blur(18px);
  color: #e2e8f0;
}

.chat-status {
  display: flex;
  gap: 12px;
  padding: 16px 20px;
  border-bottom: 1px solid rgba(148, 163, 184, 0.15);
  align-items: center;
}

.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  border-radius: 999px;
  font-size: 0.9rem;
  background: rgba(148, 163, 184, 0.18);
  color: #94a3b8;
}

.status-pill.connected {
  background: rgba(34, 197, 94, 0.15);
  color: #4ade80;
}

.status-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #ef4444;
  box-shadow: 0 0 12px rgba(239, 68, 68, 0.8);
}

.status-dot.connected {
  background: #22c55e;
  box-shadow: 0 0 12px rgba(34, 197, 94, 0.8);
}

.status-note {
  margin-left: auto;
  font-size: 0.85rem;
  color: #fbbf24;
}

.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.message-card {
  padding: 12px 16px;
  border-radius: 12px;
  background: rgba(15, 23, 42, 0.65);
  border: 1px solid rgba(148, 163, 184, 0.15);
  box-shadow: 0 8px 20px rgba(2, 6, 23, 0.45);
}

.message-meta {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-bottom: 6px;
}

.message-sender {
  color: #93c5fd;
}

.message-tag {
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 0.75rem;
  text-transform: uppercase;
}

.message-tag.client {
  background: rgba(14, 165, 233, 0.2);
  color: #38bdf8;
}

.message-tag.upstream {
  background: rgba(59, 130, 246, 0.2);
  color: #93c5fd;
}

.message-tag.system {
  background: rgba(148, 163, 184, 0.2);
  color: #cbd5f5;
}

.message-text {
  margin: 0;
  color: #e2e8f0;
  line-height: 1.5;
}

.opacity-panel {
  padding: 18px 20px;
  border-top: 1px solid rgba(148, 163, 184, 0.15);
  border-bottom: 1px solid rgba(148, 163, 184, 0.15);
  background: rgba(15, 23, 42, 0.55);
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
}

.panel-title {
  font-weight: 600;
  color: #f8fafc;
  flex-basis: 100%;
}

.slider-label {
  display: flex;
  align-items: center;
  gap: 12px;
  flex: 1;
  color: #cbd5f5;
}

.slider-label input[type="range"] {
  flex: 1;
}

.slider-value {
  width: 50px;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.composer {
  padding: 16px 20px;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
  align-items: center;
  border-top: 1px solid rgba(148, 163, 184, 0.15);
}

.auth-hint {
  padding: 10px 14px;
  border-radius: 10px;
  text-align: center;
  font-size: 0.9rem;
  border: 1px dashed rgba(248, 250, 252, 0.4);
}

.auth-hint.ready {
  background: rgba(34, 197, 94, 0.15);
  color: #4ade80;
  border-color: rgba(34, 197, 94, 0.4);
}

.text-input {
  width: 100%;
  padding: 12px 14px;
  border-radius: 10px;
  border: 1px solid rgba(148, 163, 184, 0.25);
  background: rgba(15, 23, 42, 0.4);
  color: #f1f5f9;
  font-size: 1rem;
}

.text-input:focus {
  outline: none;
  border-color: rgba(99, 102, 241, 0.7);
  box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.25);
}

.primary-btn {
  padding: 12px 20px;
  border: none;
  border-radius: 12px;
  background: linear-gradient(120deg, #6366f1, #8b5cf6);
  color: #fff;
  font-weight: 600;
  cursor: pointer;
  transition: transform 0.2s ease, box-shadow 0.2s ease;
}

.primary-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.primary-btn:not(:disabled):hover {
  transform: translateY(-1px);
  box-shadow: 0 12px 30px rgba(79, 70, 229, 0.35);
}

.error-text {
  grid-column: 1 / -1;
  color: #f87171;
  font-size: 0.9rem;
}

.char-counter {
  text-align: right;
  padding: 0 20px 16px;
  color: #94a3b8;
  font-size: 0.85rem;
  letter-spacing: 0.05em;
}
</style>
