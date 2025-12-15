<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import ChatClient from '../components/ChatClient.vue'

const route = useRoute()
const roomId = computed(() => route.params.roomId ?? 'default')
const authKey = computed(() => {
  const value = route.query.key
  if (Array.isArray(value))
    return value[0] || ''
  return typeof value === 'string' ? value : ''
})
</script>

<template>
  <section class="chat-view">
    <ChatClient :room-id="roomId" :auth-key="authKey" />
  </section>
</template>

<style scoped>
.chat-view {
  width: 100vw;
  height: 100vh;
  background: radial-gradient(circle at top, #111c44, #060919 55%, #020410);
}
</style>
