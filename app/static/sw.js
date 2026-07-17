// Argo service worker — push a schermo spento (§4.16) + shell PWA.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('push', (event) => {
  let data = { title: 'Argo', body: '' };
  try { if (event.data) data = event.data.json(); }
  catch (e) { if (event.data) data = { title: 'Argo', body: event.data.text() }; }
  event.waitUntil(self.registration.showNotification(data.title || 'Argo', {
    body: data.body || '',
    tag: data.tag || 'argo',
    renotify: true,
    requireInteraction: true,
    data: { url: data.url || '/' },
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true })
    .then((wins) => {
      for (const w of wins) {
        if (w.url.includes(url) && 'focus' in w) return w.focus();
      }
      if (clients.openWindow) return clients.openWindow(url);
    }));
});
