export interface EmoteMessage {
    type: "emote"
    senderId: string
    sender: string
    is_special: boolean
    emote_url: string
}

interface CacheEntry {
    img: HTMLImageElement
    expiresAt: number
}

export class EmojiManager {
    private cache = new Map<string, CacheEntry>()
    private loading = new Map<string, Promise<void>>()
    private ttl: number // ms

    constructor(ttl: number = 60 * 1000) { // 默认缓存 60s
        this.ttl = ttl
    }

    private isExpired(entry: CacheEntry): boolean {
        return Date.now() > entry.expiresAt
    }

    isReady(url: string): boolean {
        const entry = this.cache.get(url)
        if (!entry) return false
        if (this.isExpired(entry)) {
            this.cache.delete(url)
            return false
        }
        return true
    }

    get(url: string): HTMLImageElement | undefined {
        const entry = this.cache.get(url)
        if (!entry) return undefined
        if (this.isExpired(entry)) {
            this.cache.delete(url)
            return undefined
        }
        return entry.img
    }

    prepare(url: string): Promise<void> {
        if (this.isReady(url)) {
            return Promise.resolve()
        }

        if (this.loading.has(url)) {
            return this.loading.get(url)!
        }

        const img = new Image()
        img.src = `/api/emoji/${url}`

        const p = img.decode().then(() => {
            this.cache.set(url, {
                img,
                expiresAt: Date.now() + this.ttl
            })
            this.loading.delete(url)
        })

        this.loading.set(url, p)
        return p
    }

    preload(urls: string[]) {
        urls.forEach((url) => this.prepare(url))
    }

    cleanup() {
        const now = Date.now()
        for (const [key, entry] of this.cache.entries()) {
            if (entry.expiresAt <= now) {
                this.cache.delete(key)
            }
        }
    }
}

// === 队列调度器 ===

type EmitEmoteFn = (msg: EmoteMessage, img: HTMLImageElement) => void

interface Options {
    maxPerFrame?: number
    maxQueueSize?: number
    maxRandomDelay?: number // ms，随机延迟上限
}

interface QueueEntry {
    msg: EmoteMessage
    emitTime: number
}

export class EmojiQueueScheduler {
    private queue: QueueEntry[] = []
    private running = false

    private maxPerFrame: number
    private maxQueueSize: number
    private maxRandomDelay: number

    constructor(
        private emojiManager: EmojiManager,
        private emitEmote: EmitEmoteFn,
        options?: Options
    ) {
        this.maxPerFrame = options?.maxPerFrame ?? 4
        this.maxQueueSize = options?.maxQueueSize ?? 500
        this.maxRandomDelay = options?.maxRandomDelay ?? 5000 // 默认随机延迟 0~100ms
    }

    sendEmote(msg: EmoteMessage) {
        if (this.queue.length >= this.maxQueueSize) {
            this.queue.shift() // 背压策略
        }

        const delay = 1 + Math.random() * this.maxRandomDelay
        const entry: QueueEntry = {
            msg,
            emitTime: Date.now() + delay
        }

        this.queue.push(entry)
    }

    start() {
        if (this.running) return
        this.running = true
        this.loop()
    }

    stop() {
        this.running = false
    }

    private loop = () => {
        if (!this.running) return

        let count = 0
        const now = Date.now()

        for (let i = 0; i < this.queue.length && count < this.maxPerFrame; i++) {
            const entry = this.queue[i]
            const msg = entry.msg
            const key = msg.emote_url

            if (!this.emojiManager.isReady(key)) {
                this.emojiManager.prepare(key) // 预加载
                continue
            }

            if (entry.emitTime > now) continue // 尚未到延迟时间

            const base = this.emojiManager.get(key)!
            const img = base.cloneNode(true) as HTMLImageElement
            this.emitEmote(msg, img)

            this.queue.splice(i, 1)
            i-- // 索引调整
            count++
        }

        requestAnimationFrame(this.loop)
    }
}