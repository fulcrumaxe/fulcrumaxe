// Service worker — app shell caching only (no API data)
// This file is registered by main.tsx via /sw.js

const CACHE_VERSION = 'v1'
const CACHE_NAME = `fulcrumaxe-shell-${CACHE_VERSION}`

const SHELL_ASSETS = [
  '/',
  '/index.html',
  '/manifest.json',
]

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const sw = self as any

sw.addEventListener('install', (event: ExtendableEvent) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(SHELL_ASSETS))
  )
  sw.skipWaiting()
})

sw.addEventListener('activate', (event: ExtendableEvent) => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys
          .filter(k => k !== CACHE_NAME)
          .map(k => caches.delete(k))
      )
    )
  )
  sw.clients.claim()
})

sw.addEventListener('fetch', (event: FetchEvent) => {
  // Only cache GET requests for shell assets; let API requests pass through
  const { request } = event
  const url = new URL(request.url)

  if (request.method !== 'GET') return
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/auth/') || url.pathname === '/ws') return

  event.respondWith(
    caches.match(request).then(cached => cached ?? fetch(request))
  )
})
