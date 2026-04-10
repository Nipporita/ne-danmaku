import BalanceQuery from '../views/BalanceQuery.vue'
import ChatView from '../views/ChatView.vue'
import DanmakuView from '../views/DanmakuView.vue'
import EmoteAliasAdmin from '../views/EmoteAliasAdmin.vue'

const routes = [
  {
    path: '/danmaku/chat/:roomId?',
    name: 'ChatView',
    component: ChatView,
  },
  {
    path: '/danmaku/:roomId?',
    name: 'DanmakuView',
    component: DanmakuView,
  },
  {
    path: '/balance',
    name: 'BalanceQuery',
    component: BalanceQuery,
  },
  {
    path: '/emote-alias',
    name: 'EmoteAliasAdmin',
    component: EmoteAliasAdmin,
  },
  {
    path: '/',
    redirect: '/danmaku/default',
  },
]

export default routes
