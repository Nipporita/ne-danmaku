<script setup>
import Danmaku from 'danmaku'
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { z } from 'zod'
import { AssetManager, AssetReadyQueue } from './ResourceManager'

import SuperChatDisplay from './SuperChatDisplay.vue'
import GiftDisplay from './GiftDisplay.vue'

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
const scRef = ref(null)
const giftRef = ref(null)
const danmakuResizeCleanup = ref(null)
const roomSettings = ref({
  overlay_opacity: 100,
  enable_external_emoji: true,
  enable_internal_emoji: true,
  enable_superchat: true,
  enable_gift: true,
  bind_position: true,
})

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

const assetManager = new AssetManager()
const assetReadyQueue = new AssetReadyQueue(assetManager, {
  maxConcurrent: 6,
})

function resolveEmoteUrl(emoteUrl) {
  // Internal emotes already have a full path; external ones need the emoji proxy prefix
  return emoteUrl.startsWith('/') ? emoteUrl : `/api/emoji/${emoteUrl}`
}

// 后端把消息文本里的行内元素（<face id="339" type="2" platform="llonebot"/> 之类）
// 切成了 msg.inline（文字段 + 元素段）。这里把"文字 + 表情图"拼成一个 DOM 元素，
// 交给 danmaku 库的 render 钩子渲染——和表情包走的是同一条路。
// 不认识的元素、图挂了、房间关掉了外部表情 → 一律显示 █（后端 text_plain 也是这个字符）。
const INLINE_PLACEHOLDER = '█'

function buildInlineNode(msg, config, size, color) {
  const makePlaceholder = () => {
    const span = document.createElement('span')
    span.textContent = INLINE_PLACEHOLDER
    return span
  }
  const wrap = document.createElement('div')
  wrap.style.cssText = [
    'display:flex',
    'align-items:center',
    'gap:0.08em',
    'white-space:pre',
    `font-family:${config.font}`,
    `font-size:${size}px`,
    'font-weight:bold',
    `color:${color}`,
    'text-shadow:#000 1px 0px 1px, #000 0px 1px 1px, #000 0px -1px 1px, #000 -1px 0px 1px',
    `opacity:${overlayOpacity.value ?? 1}`,
  ].join(';')

  for (const seg of msg.inline) {
    if (seg.kind === 'text') {
      if (!seg.text)
        continue
      const span = document.createElement('span')
      span.textContent = seg.text // 用 textContent，绝不拼 HTML（弹幕是用户输入）
      wrap.appendChild(span)
      continue
    }
    const url = seg.url || seg.url_alt
    if (seg.kind === 'face' && url && roomSettings.value.enable_external_emoji) {
      const img = document.createElement('img')
      img.src = resolveEmoteUrl(url)
      img.alt = ''
      img.style.cssText = `width:${size}px;height:${size}px;object-fit:contain;flex:0 0 auto`
      let triedAlt = false
      img.onerror = () => {
        // 主地址挂了（腾讯那边 id 规则不同）→ 换备用地址再试一次，最后退回 █
        if (!triedAlt && seg.url_alt && resolveEmoteUrl(seg.url_alt) !== img.src) {
          triedAlt = true
          img.src = resolveEmoteUrl(seg.url_alt)
          return
        }
        img.replaceWith(makePlaceholder())
      }
      wrap.appendChild(img)
      continue
    }
    wrap.appendChild(makePlaceholder())
  }
  // 一段都没有（理论上不会）→ 至少给个占位，别渲染出一个空弹幕
  if (!wrap.childNodes.length)
    wrap.appendChild(makePlaceholder())
  return wrap
}

function hasInlineElements(msg) {
  return Array.isArray(msg.inline) && msg.inline.some(seg => seg.kind !== 'text')
}

function applyRoomSettings(settings) {
  roomSettings.value = {
    overlay_opacity: Number(settings?.overlay_opacity ?? 100),
    enable_external_emoji: Boolean(settings?.enable_external_emoji ?? true),
    enable_internal_emoji: Boolean(settings?.enable_internal_emoji ?? true),
    enable_superchat: Boolean(settings?.enable_superchat ?? true),
    enable_gift: Boolean(settings?.enable_gift ?? true),
    bind_position: Boolean(settings?.bind_position ?? true),
  }
  const value = Math.max(0, Math.min(100, roomSettings.value.overlay_opacity))
  overlayOpacity.value = value / 100
}

function handleEmoji(msg) {
  const urls_raw = msg.emote_urls ?? [msg.emote_url]
  const urls = urls_raw.map(resolveEmoteUrl).slice(0, 5) // 限制最多5个表情，避免过度占用资源

  for (let i = 0; i < urls.length; i++) {
    const url = urls[i]
    const task = {
      id: `${url}-${new Date().getTime()}-${i}`,
      resourceKeys: [url],
      payload: { ...msg, _resolvedUrl: url },
      onReady: (payload) => {
        const img = new Image()
        img.src = payload._resolvedUrl
        setTimeout(() => emitEmoji(payload, img), 1000 * i)
      },
    }
    assetReadyQueue.enqueue(task)
  }
}

function emitEmoji(msg, img) {
  const config = getConfig()
  const size = msg.size ?? config.defaultSize

  const payload = {
    render: () => {
      img.style.width = `${2 * size}px`
      img.style.height = `${2 * size}px`
      img.style.display = 'block'
      img.style.objectFit = 'contain'
      img.style.opacity = overlayOpacity.value ?? 1
      return img
    },
  }

  danmaku.value.emit(payload)
}

function handleSC(msg) {
  if (!roomSettings.value.enable_superchat)
    return
  const task = {
    id: msg.avatar_url,
    resourceKeys: [`/api/emoji/${msg.avatar_url}`],
    payload: msg,
    onReady: emitSC,
  }
  assetReadyQueue.enqueue(task)
}

function emitSC(msg) {
  scRef.value.addSC({
    id: msg.sender + msg.text + new Date().getTime(),
    user: msg.sender,
    amount: msg.cost,
    text: msg.text,
    ttl: msg.duration * 1000,
    avatar: `/api/emoji/${msg.avatar_url}`,
  })
}

function handleGift(msg) {
  if (!roomSettings.value.enable_gift)
    return
  const task = {
    id: msg.avatar_url,
    resourceKeys: [`/api/emoji/${msg.avatar_url}`],
    payload: msg,
    onReady: emitGift,
  }
  assetReadyQueue.enqueue(task)
}

function emitGift(msg) {
  giftRef.value.addGift({
    id: msg.sender + msg.gift_name + new Date().getTime(),
    user: msg.sender,
    gift_name: msg.gift_name,
    quantity: msg.quantity,
    cost: msg.cost,
    image_url: msg.image_url,
    avatar: `/api/emoji/${msg.avatar_url}`,
  })
}

function resolveMode(msg) {
  const incoming = msg.position
  if (!roomSettings.value.bind_position)
    return 'rtl'
  if (incoming === 'top' || incoming === 'bottom' || incoming === 'rtl')
    return incoming
  if (incoming === 'scroll')
    return 'rtl'
  return 'rtl'
}

function sendMessage(msg) {
  if (!danmaku.value)
    return

  // 被标记为 blocked 的消息不渲染到弹幕层（但仍会到达 WebSocket，游戏服务器可使用）
  if (msg.blocked)
    return

  const config = getConfig()
  const color = msg.color ?? config.defaultColor
  const size = msg.size ?? config.defaultSize
  const mode = resolveMode(msg)
  const type = msg.type ?? 'plain'

  if (type === 'plain') {
    const payload = {
      mode,
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
    // 文本里带行内元素（表情之类）→ 用 render 钩子画"文字 + 图片"
    if (hasInlineElements(msg))
      payload.render = () => buildInlineNode(msg, config, size, color)
    danmaku.value.emit(payload)
  }
  else if (type === 'emote' || type === 'multi_emote') {
    handleEmoji(msg)
  }
  else if (type === 'superchat') {
    handleSC(msg)
  }
  else if (type === 'gift') {
    handleGift(msg)
  }
}

function clearAllOverlays() {
  scRef.value?.clearAll?.()
  giftRef.value?.clearAll?.()
  danmakuResizeCleanup.value?.()
  danmakuResizeCleanup.value = null
  danmaku.value?.destroy()
  danmaku.value = null
  initDanmaku()
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
    if (data?.type === 'settings' && data?.settings) {
      applyRoomSettings(data.settings)
      return
    }
    if (data?.type === 'control' && data?.action === 'clear_all') {
      clearAllOverlays()
      return
    }
    // 配置热重载广播：本组件不消费配置，但必须吞掉这一帧，
    // 否则会落到下面的 sendMessage() 被当成一条 text 为 undefined 的弹幕
    if (data?.type === 'config') {
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

  danmakuResizeCleanup.value?.()
  danmakuResizeCleanup.value = null
  danmaku.value?.destroy()
  danmaku.value = null

  const config = getConfig()
  danmaku.value = new Danmaku({
    container: containerRef.value,
    engine: 'dom',
    speed: config.speed,
  })
  danmaku.value.show()

  const handleResize = () => danmaku.value?.resize()
  window.addEventListener('resize', handleResize)
  danmakuResizeCleanup.value = () => {
    window.removeEventListener('resize', handleResize)
  }
}

onMounted(() => {
  initDanmaku()
  connectWebSocket()
  // 定时清理过期缓存资源
  assetManager._sweepTimer = setInterval(() => assetManager.sweepExpired(), 60000)
})

onUnmounted(() => {
  if (assetManager._sweepTimer) clearInterval(assetManager._sweepTimer)
  danmakuResizeCleanup.value?.()
  danmakuResizeCleanup.value = null
  socket.value?.close()
  danmaku.value?.destroy()
  danmaku.value = null
})

watch(() => props.roomId, () => {
  socket.value?.close()
  applyRoomSettings({})
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
  <SuperChatDisplay ref="scRef" />
  <GiftDisplay ref="giftRef" />
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
