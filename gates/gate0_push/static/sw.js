// GATE 0 service worker — riceve la push e la mostra anche a schermo spento.
// Su iOS 16.4+ questo funziona SOLO se la PWA e' stata "Aggiungi a Home" (§1.10).

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', (event) => {
  let data = { title: 'Argo', body: 'push senza payload' };
  try {
    if (event.data) data = event.data.json();
  } catch (e) {
    if (event.data) data = { title: 'Argo', body: event.data.text() };
  }
  const title = data.title || 'Argo GATE 0';
  const options = {
    body: data.body || '',
    tag: data.tag || 'argo-gate0',
    renotify: true,
    requireInteraction: true,      // resta finche' non la tocchi
    data: { url: data.url || '/' },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
      for (const w of wins) {
        if ('focus' in w) return w.focus();
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});
