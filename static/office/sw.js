'use strict';
self.addEventListener('install', event => event.waitUntil(self.skipWaiting()));
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', event => {
  const request = event.request, url = new URL(request.url);
  if (request.mode !== 'navigate' || request.method !== 'GET' || url.origin !== location.origin || !url.pathname.startsWith('/office/')) return;
  event.respondWith(fetch(request,{cache:'no-store'}).catch(() => new Response(
    '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Кабинет филиала</title><p>Отсутствует подключение к интернету</p><script>addEventListener("online",()=>location.reload())</script></html>',
    {status:503,headers:{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store'}})));
});
function destination(value) {
  return typeof value === 'string' && /^\/office\/messages\?thread=[a-zA-Z0-9_-]{1,80}$/.test(value) ?
    value : '/office/notifications';
}
self.addEventListener('push', event => {
  let payload; try { payload = event.data?.json(); } catch {}
  event.waitUntil(self.registration.showNotification('GateBot · Кабинет',{
    body:'Откройте кабинет филиала, чтобы посмотреть новое событие.',
    icon:'/ui/branding/default/icon-192.png',data:{url:destination(payload?.url)}}));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const target = new URL(destination(event.notification.data?.url),location.origin).href;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({type:'window',includeUncontrolled:true});
    for (const client of windows) {
      const url = new URL(client.url);
      if (url.origin===location.origin && url.pathname.startsWith('/office/')) {
        const navigated = await client.navigate(target); return (navigated || client).focus();
      }
    }
    return self.clients.openWindow(target);
  })());
});
