const CACHE_NAME = 'invest-signal-v1';
const CACHE_FILES = [
  './', './index.html', './manifest.json',
  './icon-192.png', './icon-512.png', './icon-180.png'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache =>
      cache.addAll(CACHE_FILES).catch(err => console.warn('Cache partial fail:', err))
    )
  );
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  const url = event.request.url;
  // データ系（data.json / analysis.json / vapid.json / api）は常に最新を優先
  if (url.includes('data.json') || url.includes('analysis.json') ||
      url.includes('vapid.json') || url.includes('/api/')) {
    event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
    return;
  }
  event.respondWith(
    caches.match(event.request).then(cached => {
      if (url.includes('index.html') || url.endsWith('/')) {
        return fetch(event.request).then(res => {
          caches.open(CACHE_NAME).then(c => c.put(event.request, res.clone()));
          return res;
        }).catch(() => cached);
      }
      return cached || fetch(event.request);
    })
  );
});

// ===== プッシュ通知受信 =====
self.addEventListener('push', event => {
  let payload = {title: '投資シグナル', body: 'シグナルが更新されました'};
  try { if (event.data) payload = event.data.json(); } catch (e) {}
  event.waitUntil(
    self.registration.showNotification(payload.title || '投資シグナル', {
      body: payload.body || '',
      icon: 'icon-192.png',
      badge: 'icon-192.png',
      data: payload,
      tag: 'invest-signal',
      renotify: true
    })
  );
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({type: 'window', includeUncontrolled: true}).then(list => {
      for (const c of list) { if ('focus' in c) return c.focus(); }
      if (clients.openWindow) return clients.openWindow('./index.html');
    })
  );
});
