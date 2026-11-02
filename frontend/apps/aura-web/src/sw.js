/* Static-shell-only service worker. Runtime transcripts and drafts are never cached. */
const CACHE_NAME = 'aura-static-shell-v1';
const INJECTED_MANIFEST = self.__WB_MANIFEST || [];
const STATIC_SHELL = ['/', '/index.html', '/manifest.webmanifest'];

self.addEventListener('install', (event) => {
  const urls = [...STATIC_SHELL, ...INJECTED_MANIFEST.map((entry) => entry.url)];
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll([...new Set(urls)])));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  // Authenticated API responses, SSE, transcripts, and drafts never enter the
  // static-shell cache. The server remains the source of truth after reload.
  if (event.request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/') || url.pathname.startsWith('/health/')) return;
  event.respondWith(caches.match(event.request).then((cached) => cached ?? fetch(event.request)));
});
