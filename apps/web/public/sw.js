/* CZCE Radar Service Worker
 * 静态资源：缓存优先（ stale-while-revalidate ）；API：网络优先，失败回缓存。
 * 版本号随构建手工 bump（发布脚本/提交时更新 BUILD 即可强制刷新缓存）。
 */

const BUILD = 'm6-20261111-3'
const STATIC_CACHE = `static-${BUILD}`
const API_CACHE = `api-${BUILD}`

const PRECACHE = ['/', '/index.html', '/manifest.webmanifest']

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting()),
  )
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key !== STATIC_CACHE && key !== API_CACHE)
            .map((key) => caches.delete(key)),
        ),
      )
      .then(() => self.clients.claim()),
  )
})

function isApi(url) {
  return url.pathname.startsWith('/api/') || url.pathname === '/ready' || url.pathname === '/live'
}

async function networkFirst(request) {
  const cache = await caches.open(API_CACHE)
  try {
    const response = await fetch(request)
    if (!response.ok) {
      // 5xx/4xx 同样回退缓存（如 Render 休眠唤醒期）
      throw new Error(`HTTP ${response.status}`)
    }
    await cache.put(request, response.clone())
    return response
  } catch (error) {
    const cached = await cache.match(request)
    if (cached) {
      // 标记来源，前端据此提示「数据可能过期」
      const headers = new Headers(cached.headers)
      headers.set('x-sw-cache', '1')
      return new Response(cached.body, {
        status: cached.status,
        statusText: cached.statusText,
        headers,
      })
    }
    throw error
  }
}

async function cacheFirst(request) {
  const cached = await caches.match(request)
  const refresh = fetch(request)
    .then(async (response) => {
      if (response.ok) {
        const cache = await caches.open(STATIC_CACHE)
        await cache.put(request, response.clone())
      }
      return response
    })
    .catch(() => null)
  if (cached) {
    // 后台刷新，先返回缓存
    void refresh
    return cached
  }
  const response = await refresh
  if (response) return response
  throw new Error('offline and no cache')
}

self.addEventListener('fetch', (event) => {
  const { request } = event
  if (request.method !== 'GET') return
  const url = new URL(request.url)
  if (url.origin !== self.location.origin) return

  // SSE 长连接不经过 SW，直接放行
  if (url.pathname.startsWith('/api/v1/stream/')) return

  if (isApi(url)) {
    event.respondWith(networkFirst(request))
    return
  }

  // SPA 路由回退到 index.html（离线可用）
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() =>
        caches.match('/index.html').then((cached) => cached || Promise.reject(new Error('offline'))),
      ),
    )
    return
  }

  event.respondWith(cacheFirst(request))
})
