<script setup>
import Danmaku from 'danmaku'
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { z } from 'zod'

const props = defineProps({
  roomId: {
    type: String,
    required: true,
  },
})

const containerRef = ref(null)
const loading = ref(true)
const danmaku = ref(null)
const socket = ref(null)
const reconnectAttempts = ref(0)
const overlayOpacity = ref(1)

const configSchema = z.object({
  defaultColor: z.string().catch('white'),
  defaultSize: z.coerce.number().catch(40),
  speed: z.coerce.number().catch(144),
  font: z.string().catch('sans-serif'),
})

function getConfig() {
  const params = new URLSearchParams(window.location.search)
  return configSchema.parse(Object.fromEntries(params))
}

function sendMessage(msg) {
  if (!danmaku.value)
    return

  const config = getConfig()
  const color = msg.color ?? config.defaultColor
  const size = msg.size ?? config.defaultSize

  const payload = {
    text: msg.text,
    style: {
      fontFamily: config.font,
      fontSize: `${size}px`,
      fontWeight: 'bold',
      color,
      textShadow: '#000 1px 0px 1px, #000 0px 1px 1px, #000 0px -1px 1px, #000 -1px 0px 1px',
      opacity: overlayOpacity.value ?? 1,
    },
  }

  danmaku.value.emit(payload)
}

function connectWebSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss://' : 'ws://'
  const wsUrl = `${protocol}${window.location.host}/api/danmaku/v1/danmaku/${props.roomId}`

  socket.value = new WebSocket(wsUrl)

  socket.value.onopen = () => {
    reconnectAttempts.value = 0
    loading.value = false
    sendMessage({ text: '元火弹幕姬已连接~' })
  }

  socket.value.onmessage = (event) => {
    const data = JSON.parse(event.data)

    if (data?.control?.type === 'setOpacity') {
      const value = Math.max(0, Math.min(100, Number(data.control.value) || 0))
      overlayOpacity.value = value / 100
      return
    }

    sendMessage(data)
  }

  socket.value.onclose = () => {
    loading.value = true
    sendMessage({ text: '元火弹幕姬已断开~' })

    const reconnectDelay = Math.min(30000, 2 ** reconnectAttempts.value * 1000)
    setTimeout(connectWebSocket, reconnectDelay)
    reconnectAttempts.value++
  }

  socket.value.onerror = () => {
    socket.value.close()
  }
}

function initDanmaku() {
  if (!containerRef.value)
    return

  const config = getConfig()
  danmaku.value = new Danmaku({
    container: containerRef.value,
    engine: 'dom',
    speed: config.speed,
  })
  danmaku.value.show()

  const handleResize = () => danmaku.value?.resize()
  window.addEventListener('resize', handleResize)

  return () => {
    window.removeEventListener('resize', handleResize)
  }
}

onMounted(() => {
  const cleanup = initDanmaku()
  connectWebSocket()

  onUnmounted(() => {
    cleanup?.()
    socket.value?.close()
    danmaku.value?.destroy()
  })
})

watch(() => props.roomId, () => {
  socket.value?.close()
  overlayOpacity.value = 1
  connectWebSocket()
})
</script>

<template>
  <div ref="containerRef" class="danmaku-container">
    <div v-if="loading" class="danmaku-loading">
      <div class="spinner" />
      <p>加载中...</p>
    </div>
  </div>
</template>

<style scoped>
.danmaku-container {
  width: 100%;
  height: 100%;
  overflow: hidden;
  position: relative;
  background: transparent;
}

.danmaku-loading {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  background: linear-gradient(180deg, rgba(3, 7, 18, 0.85), rgba(15, 23, 42, 0.85));
  color: #94a3b8;
  text-align: center;
  letter-spacing: 0.05em;
}

.spinner {
  width: 48px;
  height: 48px;
  border-radius: 50%;
  border: 4px solid rgba(148, 163, 184, 0.3);
  border-top-color: #38bdf8;
  animation: spin 1s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
