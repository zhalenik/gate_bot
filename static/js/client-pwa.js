'use strict';
(() => {
    const bootstrap=document.getElementById('clientPwaConfig');
    if(!bootstrap)return;
    let config;try{config=JSON.parse(bootstrap.textContent);}catch{return;}
    const scope=new URL(config.scope,location.origin),gate=new URL(config.gate_url,location.origin);
    if(scope.origin!==location.origin||!/^\/pwa\/[^/]+\/$/.test(scope.pathname)||gate.origin!==location.origin||gate.pathname!==scope.pathname+'gate')return;
    const preferencePath=config.preferences_url&&new URL(config.preferences_url,location.origin);
    const preferencesUrl=preferencePath&&preferencePath.origin===location.origin&&preferencePath.pathname===scope.pathname+'preferences'?preferencePath.pathname:null;
    const display=matchMedia('(display-mode: standalone)');
    const installed=()=>display.matches||navigator.standalone===true;
    const supported=()=>isSecureContext&&'serviceWorker' in navigator&&'PushManager' in window&&'Notification' in window;
    const el=(tag,text='',className='')=>{const node=document.createElement(tag);node.textContent=text;if(className)node.className=className;return node;};
    const tools=document.querySelector('[data-client-pwa-tools]');
    let offline=false,probeBusy=false,deferredInstall=null,registrationPromise=null,localSubscription=null;
    let pushState={supported:false},promptKey='',prompted=false,pushBusy=false,installCard=null,tabs=null;
    const offlineNotice=el('div','Отсутствует подключение к интернету');offlineNotice.id='clientOfflineNotice';offlineNotice.hidden=true;offlineNotice.setAttribute('role','status');document.body.append(offlineNotice);

    function publish(reason='state',preferences=null){
        window.dispatchEvent(new CustomEvent('gatebot:pwa-change',{detail:{reason,preferences}}));
    }
    function networkLost(){
        if(offline)return;offline=true;offlineNotice.hidden=false;document.body.classList.add('client-pwa-offline');
        document.querySelectorAll('dialog[open]').forEach(dialog=>dialog.close());publish('offline');
    }
    async function probe(){
        if(!offline||probeBusy||navigator.onLine===false)return;
        probeBusy=true;
        try{
            // Connectivity only. Never retry a gate POST or any other mutation.
            const response=await fetch(gate.pathname,{method:'HEAD',cache:'no-store',credentials:'same-origin'});
            if(response.status!==503)location.reload();
        }catch{}finally{probeBusy=false;}
    }
    addEventListener('offline',networkLost);addEventListener('online',()=>{if(offline)probe();});
    addEventListener('gatebot:network-error',networkLost);
    document.addEventListener('click',event=>{if(offline){event.preventDefault();event.stopImmediatePropagation();}},true);
    if(navigator.onLine===false)networkLost();
    const reconnectTimer=setInterval(probe,5000);addEventListener('pagehide',()=>clearInterval(reconnectTimer),{once:true});

    async function request(path,payload,retryCsrf=true){
        if(offline||navigator.onLine===false)throw new Error('Отсутствует подключение к интернету');
        const options={credentials:'same-origin',cache:'no-store'};
        if(payload!==undefined){options.method='POST';options.headers={'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]')?.content||''};options.body=JSON.stringify(payload);}
        let response;try{response=await fetch(path,options);}catch{networkLost();throw new Error('Отсутствует подключение к интернету');}
        let result;try{result=await response.json();}catch{throw new Error('Не удалось получить ответ сервера. Обновите страницу.');}
        if(result.csrf_token){const meta=document.querySelector('meta[name="csrf-token"]');if(meta)meta.content=result.csrf_token;}
        if(!response.ok){
            if(payload!==undefined&&retryCsrf&&response.status===403&&result.code==='csrf_expired'){
                // This rejection occurs before the handler. Renew the anonymous
                // session once; never repeat an uncertain transport outcome.
                await request(scope.pathname+'push/status');return request(path,payload,false);
            }
            throw new Error(result.message||'Не удалось настроить уведомления. Повторите попытку.');
        }
        return result;
    }
    function activeRegistration(registration){
        if(registration.active?.state==='activated')return Promise.resolve(registration);
        return new Promise((resolve,reject)=>{
            const worker=registration.active||registration.installing||registration.waiting;
            if(!worker){reject(new Error('Приложение ещё не готово. Откройте его заново.'));return;}
            const timeout=setTimeout(()=>finish(new Error('Не удалось подготовить приложение. Откройте его заново.')),12000);
            function finish(error){clearTimeout(timeout);worker.removeEventListener('statechange',changed);error?reject(error):resolve(registration);}
            function changed(){if(worker.state==='activated')finish();else if(worker.state==='redundant')finish(new Error('Обновите приложение и повторите попытку.'));}
            worker.addEventListener('statechange',changed);changed();
        });
    }
    function registration(){
        if(!registrationPromise){
            registrationPromise=navigator.serviceWorker.register('/pwa/sw.js',{scope:scope.pathname,updateViaCache:'none'}).then(activeRegistration);
            registrationPromise.catch(()=>{registrationPromise=null;});
        }
        return registrationPromise;
    }
    function canPush(){return installed()&&supported()&&config.allow_push!==false&&pushState.supported===true;}
    async function refreshPush(){
        if(!installed()||!supported()||config.allow_push===false){publish();return pushState;}
        pushState=await request(scope.pathname+'push/status');
        if(pushState.supported){
            const reg=await registration();
            localSubscription=await reg.pushManager.getSubscription();
            if(localSubscription && !window.GateBotPushSubscription.matchesKey(localSubscription,pushState.public_key))localSubscription=null;
        }
        publish();return pushState;
    }
    function rememberPrompt(value){prompted=true;if(promptKey)try{localStorage.setItem(promptKey,value);}catch{}}
    async function enablePush(){
        if(pushBusy)throw new Error('Настройка уведомлений уже выполняется.');
        if(!canPush())throw new Error('Откройте установленный GateBot на устройстве с поддержкой уведомлений.');
        if(pushState.preferences?.delivery_enabled===false)throw new Error('Уведомления отключены администратором');
        if(offline||navigator.onLine===false)throw new Error('Отсутствует подключение к интернету');
        if(Notification.permission==='denied'){rememberPrompt('denied');throw new Error('Уведомления запрещены. Разрешите их для GateBot в настройках устройства.');}
        // This call deliberately precedes every await: Safari requires a direct
        // user gesture, supplied by the consent button or the Push switch.
        const permission=Notification.permission==='granted'?Promise.resolve('granted'):Notification.requestPermission();
        pushBusy=true;
        try{
            if(await permission!=='granted'){rememberPrompt('declined');publish();throw new Error('Уведомления не включены. Вы сможете разрешить их позже в настройках.');}
            const reg=await registration();
            localSubscription=await window.GateBotPushSubscription.subscribe(reg,pushState.public_key);
            const result=await request(scope.pathname+'push/subscribe',{subscription:localSubscription.toJSON(),consent:true});
            rememberPrompt('accepted');pushState={...pushState,channel_enabled:true};publish('subscribed',result.preferences||null);
            return result;
        }finally{pushBusy=false;}
    }
    function dialog(title,description){
        const node=el('dialog','','client-pwa-dialog'),heading=el('h2',title),copy=el('p',description),actions=el('div','','client-pwa-dialog-actions');
        heading.id='clientPwaDialogTitle';node.setAttribute('aria-labelledby',heading.id);node.append(heading,copy,actions);document.body.append(node);
        node.addEventListener('close',()=>node.remove(),{once:true});
        const control=(text,fn,primary=false)=>{const button=el('button',text,primary?'btn-primary':'btn-action');button.type='button';button.onclick=fn;actions.append(button);return button;};
        node.showModal();return {node,copy,actions,control};
    }
    function consentPrompt(){
        if(!canPush()||pushState.preferences?.delivery_enabled===false||prompted||offline||document.querySelector('dialog[open]'))return;
        if(Notification.permission==='denied'){rememberPrompt('denied');return;}
        if(localSubscription&&pushState.channel_enabled){rememberPrompt('accepted');return;}
        prompted=true;
        const view=dialog('Уведомлять о пропуске?','GateBot сообщит, когда пропуск станет действовать. Вы сможете отключить уведомления в настройках приложения в любой момент.');
        view.node.addEventListener('cancel',()=>rememberPrompt('later'));
        const later=view.control('Не сейчас',()=>{rememberPrompt('later');view.node.close();});
        const agree=view.control('Включить уведомления',()=>{
            // Invoke first, before any asynchronous work can consume the gesture.
            const attempt=enablePush();agree.disabled=true;later.disabled=true;
            attempt.then(()=>view.node.close()).catch(error=>{
                let status=view.node.querySelector('[role=status]');if(!status){status=el('p');status.setAttribute('role','status');view.node.insertBefore(status,view.actions);}status.textContent=error.message;
            }).finally(()=>{agree.disabled=false;later.disabled=false;});
        },true);
    }
    function installationGuide(){
        const ios=/iPad|iPhone|iPod/.test(navigator.userAgent)||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1);
        const view=dialog('GateBot на главном экране',ios?'Откройте эту страницу в Safari.':'Установите персональный пропуск из меню браузера.');
        const steps=el('ol');
        for(const text of ios?['Нажмите «Поделиться» в браузере.','Выберите «На экран Домой» и нажмите «Добавить».','Откройте GateBot с главного экрана.']:['Откройте меню браузера.','Выберите «Установить приложение» или «Добавить на главный экран».','Запустите GateBot с появившегося значка.'])steps.append(el('li',text));
        view.node.insertBefore(steps,view.actions);view.control('Понятно',()=>view.node.close(),true);
    }
    async function install(){
        if(!deferredInstall){installationGuide();return;}
        const event=deferredInstall;deferredInstall=null;await event.prompt();await event.userChoice;renderShell();
    }
    function renderShell(){
        document.body.classList.toggle('client-pwa-installed',installed());
        if(tools&&!installCard){
            installCard=el('section','','client-install-card');installCard.id='clientInstallCard';
            installCard.append(el('h2','Пропуск на главном экране'),el('p','Открывайте ворота из GateBot — персональная ссылка всегда под рукой.'));
            const control=el('button','Установить GateBot','btn-primary');control.type='button';control.onclick=()=>install().catch(()=>installationGuide());installCard.append(control);tools.append(installCard);
        }
        if(installCard)installCard.hidden=installed()||!isSecureContext||!('serviceWorker' in navigator);
        if(tabs)tabs.remove();tabs=null;
        if(installed()){
            tabs=el('nav','','client-pwa-tabs');tabs.setAttribute('aria-label','GateBot');
            for(const [path,title] of [[gate.pathname,'Ворота'],[preferencesUrl,'Уведомления']]){
                if(!path)continue;const link=el('a',title);link.href=path;
                const active=location.pathname===path||(path===gate.pathname&&location.pathname.startsWith('/opn/'))||(path===preferencesUrl&&location.pathname.startsWith('/preferences/'));
                if(active)link.setAttribute('aria-current','page');tabs.append(link);
            }
            document.body.append(tabs);
            const preferenceLink=document.getElementById('preferencesLink');if(preferenceLink&&preferencesUrl)preferenceLink.href=preferencesUrl;
        }
        publish();
    }
    window.GateBotPWA={get installed(){return installed();},get online(){return !offline;},get canPush(){return canPush();},
        get deviceSubscribed(){return Boolean(localSubscription&&Notification.permission==='granted');},get preferencesUrl(){return preferencesUrl;},enablePush,refreshPush,networkLost};
    addEventListener('beforeinstallprompt',event=>{event.preventDefault();deferredInstall=event;renderShell();});
    addEventListener('appinstalled',()=>{
        deferredInstall=null;renderShell();
        if(installed())refreshPush().then(consentPrompt).catch(()=>{});
        else if(!offline){const view=dialog('GateBot установлен','Откройте GateBot с главного экрана, чтобы включить уведомления о пропуске.');view.control('Понятно',()=>view.node.close(),true);}
    });
    display.addEventListener?.('change',()=>{renderShell();refreshPush().then(consentPrompt).catch(()=>{});});
    renderShell();
    (async()=>{
        if(!isSecureContext||!('serviceWorker' in navigator))return;
        await registration();
        if(!installed()||!supported()||config.allow_push===false)return;
        // Persist only a digest and a consent marker, never the bearer token or
        // private page contents. The operating system owns the PushSubscription.
        const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(scope.pathname));
        promptKey='gatebot:pwa:consent:'+Array.from(new Uint8Array(digest),byte=>byte.toString(16).padStart(2,'0')).join('');
        try{prompted=Boolean(localStorage.getItem(promptKey));}catch{}
        await refreshPush();consentPrompt();
    })().catch(()=>{publish();});
})();
