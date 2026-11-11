/** 修订推送 SSE 客户端：since_id 断线补齐 + 指数退避（封顶 5s）+ online 立即重连。
 *
 * 断网恢复后 ≤10s 重连（退避封顶 5s + online 事件即时触发）；页面隐藏时主动断开，
 * 可见时用本地 lastId 作为 since_id 重连，服务端按 id 补齐缺口。
 */

import { apiBase } from './client'
import type { RevisionItem } from '../types/api'

export type StreamStatus = 'connecting' | 'open' | 'closed'

const MAX_BACKOFF_MS = 5000

export class RevisionStream {
  private es: EventSource | null = null
  private lastId = 0
  private retries = 0
  private timer: number | undefined
  private running = false

  constructor(
    private readonly onRevision: (item: RevisionItem) => void,
    private readonly onStatus: (status: StreamStatus) => void,
    initialLastId = 0,
  ) {
    this.lastId = initialLastId
  }

  start(): void {
    if (this.running) return
    this.running = true
    window.addEventListener('online', this.handleOnline)
    document.addEventListener('visibilitychange', this.handleVisibility)
    this.connect()
  }

  stop(): void {
    this.running = false
    window.removeEventListener('online', this.handleOnline)
    document.removeEventListener('visibilitychange', this.handleVisibility)
    this.clearTimer()
    this.close()
    this.onStatus('closed')
  }

  private handleOnline = (): void => {
    if (this.running) this.connect()
  }

  private handleVisibility = (): void => {
    if (!this.running) return
    if (document.visibilityState === 'hidden') {
      this.clearTimer()
      this.close()
      this.onStatus('closed')
    } else {
      this.connect()
    }
  }

  private connect(): void {
    this.close()
    this.clearTimer()
    this.onStatus('connecting')
    const url = `${apiBase()}/api/v1/stream/revisions?since_id=${this.lastId}`
    const es = new EventSource(url)
    this.es = es
    es.onopen = () => {
      this.retries = 0
      this.onStatus('open')
    }
    es.addEventListener('revision', (event: Event) => {
      const data = (event as MessageEvent<string>).data
      try {
        const item = JSON.parse(data) as RevisionItem
        this.lastId = Math.max(this.lastId, item.id)
        this.onRevision(item)
      } catch {
        // 非法负载直接忽略，保持流存活
      }
    })
    es.onerror = () => {
      this.close()
      this.schedule()
    }
  }

  private schedule(): void {
    if (!this.running || document.visibilityState === 'hidden') return
    this.onStatus('connecting')
    const delay = Math.min(MAX_BACKOFF_MS, 1000 * 2 ** this.retries)
    this.retries += 1
    this.timer = window.setTimeout(() => this.connect(), delay)
  }

  private close(): void {
    if (this.es !== null) {
      this.es.close()
      this.es = null
    }
  }

  private clearTimer(): void {
    if (this.timer !== undefined) {
      window.clearTimeout(this.timer)
      this.timer = undefined
    }
  }
}
