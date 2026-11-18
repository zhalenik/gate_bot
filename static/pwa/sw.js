'use strict';
// One registration per personal /pwa/<token>/ scope. Never store pages, API
// responses or commands, and never replay a failed request.
function personalPaths(){
    const scope=new URL(self.registration.scope);
    if(scope.origin!==self.location.origin||!/^\/pwa\/[^/]+\/$/.test(scope.pathname))return null;
    return {gate:scope.pathname+'gate',preferences:scope.pathname+'preferences'};
}
self.addEventListener('install',event=>event.waitUntil(self.skipWaiting()));
self.addEventListener('activate',event=>event.waitUntil(self.clients.claim()));
self.addEventListener('fetch',event=>{
    const paths=personalPaths(),url=new URL(event.request.url);
    if(!paths||event.request.method!=='GET'||event.request.mode!=='navigate'||url.origin!==self.location.origin||
       ![paths.gate,paths.preferences].includes(url.pathname))return;
    event.respondWith(fetch(event.request,{cache:'no-store'}).catch(()=>new Response(
        '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>GateBot</title><body>Отсутствует подключение к интернету<script>addEventListener("online",()=>location.reload())</script></body></html>',
        {status:503,headers:{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store','Referrer-Policy':'no-referrer'}})));
});
self.addEventListener('push',event=>{
    const paths=personalPaths();if(!paths)return;
    let payload={};try{payload=event.data?.json()||{};}catch{}
    let valid=false;
    try{const url=new URL(payload.url,self.location.origin);valid=url.origin===self.location.origin&&url.pathname===paths.gate&&!url.search&&!url.hash;}catch{}
    const body=valid&&typeof payload.body==='string'?payload.body.slice(0,500):'Откройте GateBot, чтобы проверить пропуск.';
    event.waitUntil(self.registration.showNotification('GateBot',{body,tag:'gatebot-pass',data:{url:paths.gate}}));
});
self.addEventListener('notificationclick',event=>{
    event.notification.close();const paths=personalPaths();if(!paths)return;
    // The target is rebuilt from this worker's own scope, never from a push URL.
    const target=new URL(paths.gate,self.location.origin).href;
    event.waitUntil((async()=>{
        const windows=await self.clients.matchAll({type:'window',includeUncontrolled:true});
        for(const client of windows){
            const url=new URL(client.url);
            if(url.origin===self.location.origin&&[paths.gate,paths.preferences].includes(url.pathname)){
                const navigated=await client.navigate(target);return (navigated||client).focus();
            }
        }
        return self.clients.openWindow(target);
    })());
});
