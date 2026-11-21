'use strict';

const CLIENT_CHANNEL_TITLES = {email:'Email',telegram:'Telegram Bot',max:'MAX Bot',messengers:'Мессенджеры (сообщение от администратора)',sms:'СМС',push:'Push'};
const CLIENT_CHANNEL_PATHS = {
    email:'<rect x="3" y="5" width="18" height="14" rx="3"/><path d="m4 7 8 6 8-6"/>',
    telegram:'<path d="m21 3-4 18-5-6-4 3 1-7L21 3 3 10l6 1m3 4 4-8"/>',
    max:'<path d="M5 18 3 21l6-1a9 9 0 1 0-4-2Z"/><path d="M8 15V9l4 4 4-4v6"/>',
    messengers:'<path d="M13 16H7l-4 3V6a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3v6"/><path d="M10 18a3 3 0 0 0 3 3h4l4 2V12a3 3 0 0 0-3-3h-5a3 3 0 0 0-3 3Z"/>',
    sms:'<path d="M4 4h16v13H9l-5 4V4Z"/><path d="M8 8h8M8 12h5"/>',
    push:'<path d="M6 9a6 6 0 0 1 12 0c0 7 3 7 3 9H3c0-2 3-2 3-9Zm4 12h4"/>'
};
function clientChannelIcon(channel,className){const icon=element('span',className);icon.setAttribute('aria-hidden','true');icon.title=CLIENT_CHANNEL_TITLES[channel];icon.innerHTML='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">'+CLIENT_CHANNEL_PATHS[channel]+'</svg>';return icon;}

function clientNotificationStatusButton(user) {
    const state=user.notification_status||{},active=Object.keys(CLIENT_CHANNEL_TITLES).filter(channel=>state.effective_channels?.[channel]);
    const control=button('',()=>openClientNotifications(user.token),'btn-action client-notifications-status');
    control.title=state.available===false?(state.reason==='guest'?'Для гостевого пропуска рассылка недоступна':'Настройки уведомлений временно недоступны'):active.length?active.map(channel=>CLIENT_CHANNEL_TITLES[channel]).join(', '):'Уведомления выключены — настроить';
    control.setAttribute('aria-label','Уведомления: '+(user.user_name||'клиент'));
    control.dataset.activeChannels=active.join(' ');
    control.disabled=state.available===false;
    if(active.length)for(const channel of active)control.append(clientChannelIcon(channel,'client-channel-mini'));
    else control.append(element('span','client-channel-empty',state.available===false?(state.reason==='guest'?'Гость':'Недоступно'):'Выключены'));
    return control;
}

async function openClientNotifications(token) {
    const user=usersCache.find(item=>item.token===token);if(!user)return;
    const view=createModal('Уведомления клиента');view.modal.classList.add('client-notifications-modal');view.body.classList.add('client-preferences');
    view.body.append(element('p','page-description',(user.user_name||'Клиент')+' · '+(user.phone||'')));
    const notice=element('p','form-hint'),state=element('p','preference-state'),channels=element('div','preference-channels'),connection=element('div','preference-connection');
    notice.setAttribute('role','status');state.setAttribute('role','status');connection.hidden=true;
    const stop=button('Отключить все уведомления',()=>confirmUnsubscribe(),'btn-action preference-unsubscribe');stop.hidden=true;
    view.body.append(notice,state,stop,channels,connection);
    let prefs=null,busy=false,revision=0,connecting=null;const cards={};
    const close=view.close;function cleanup(){window.removeEventListener('focus',refresh);}
    view.close=()=>{cleanup();close();};view.modal.addEventListener('keydown',event=>{if(event.key==='Escape')cleanup();});
    function tell(message,error=false){notice.textContent=message;notice.className=error?'alert alert-error':'form-hint';}
    function allowed(){return prefs?.allowed_channels||[];}
    function selected(channel,value){const choices=Object.fromEntries(allowed().map(key=>[key,Boolean(prefs.enabled&&prefs.channels[key])]));choices[channel]=value;return {enabled:value||prefs.enabled,channels:choices};}
    function closeConnection(){connecting=null;connection.hidden=true;connection.replaceChildren();}
    function syncTable(){const current=usersCache.find(item=>item.token===token);if(!current)return;current.mailing_enabled=prefs.enabled;current.email=prefs.email;current.notification_status={available:true,enabled:prefs.enabled,allowed_channels:prefs.allowed_channels,effective_channels:prefs.effective_channels};filterUsers();}
    function setBusy(value){busy=value;for(const [channel,card] of Object.entries(cards))card.input.disabled=value||prefs?.delivery_enabled===false||card.card.hidden||(channel==='push'&&!prefs?.connected.push);stop.disabled=value||prefs?.delivery_enabled===false;for(const item of connection.querySelectorAll('input,button'))item.disabled=value;refreshButton.disabled=value;}
    function paint(){
        if(!prefs||!view.modal.isConnected)return;
        for(const channel of allowed()){
            if(!cards[channel]){
                const card=element('section','preference-channel'),row=element('div','preference-channel-row'),icon=clientChannelIcon(channel,'preference-channel-icon'),copy=element('div','preference-channel-copy'),label=element('label','preference-channel-label',CLIENT_CHANNEL_TITLES[channel]),description=element('p','preference-channel-status'),input=element('input','preference-switch');
                input.type='checkbox';input.setAttribute('role','switch');input.id='admin-client-channel-'+channel;input.name=channel;label.htmlFor=input.id;icon.setAttribute('aria-hidden','true');description.id=input.id+'-status';input.setAttribute('aria-describedby',description.id);copy.append(label,description);row.append(icon,copy,input);card.append(row);channels.append(card);cards[channel]={card,input,description};
                input.addEventListener('change',async()=>{if(busy){paint();return;}const value=input.checked;if(value&&!prefs.connected[channel]){paint();connect(channel);return;}try{await save(selected(channel,value));closeConnection();}catch(error){tell(error.message,true);paint();}});
            }
        }
        const active=Boolean(prefs.enabled&&allowed().some(channel=>prefs.effective_channels[channel]));
        state.textContent=prefs.delivery_enabled===false?'Уведомления отключены администратором':active?'Уведомления отправляются по включённым каналам.':'Выберите каналы для уведомлений о пропуске.';stop.hidden=!active||prefs.delivery_enabled===false;
        for(const [channel,card] of Object.entries(cards)){
            card.card.hidden=!allowed().includes(channel);card.input.checked=Boolean(prefs.effective_channels[channel]);card.input.disabled=busy||prefs.delivery_enabled===false||card.card.hidden||(channel==='push'&&!prefs.connected.push);
            const connected=!!prefs.connected[channel];card.description.textContent=connected?(card.input.checked?'Включено':'Выключено'):(channel==='messengers'?'Сервис рассылки пока недоступен':'Включите, чтобы подключить');
            if(channel==='email'&&prefs.email)card.description.textContent+=' · '+prefs.email;
            if(channel==='push'&&!connected)card.description.textContent='Клиент подключает Push в установленном приложении';
        }
        if(connecting&&(prefs.delivery_enabled===false||!allowed().includes(connecting)))closeConnection();
        syncTable();
    }
    async function save(changes){
        const version=++revision;setBusy(true);
        try{const result=await post('/admin/api/client-notifications',{token,enabled:prefs.enabled,channels:prefs.channels,...changes});if(version===revision){prefs=result.preferences;paint();tell('Настройки сохранены');}return result;}
        finally{setBusy(false);}
    }
    async function refresh(){
        if(!view.modal.isConnected){cleanup();return;}if(busy)return;const version=++revision;setBusy(true);
        try{const result=await api('/admin/api/client-notifications?token='+encodeURIComponent(token));if(version!==revision)return;prefs=result.preferences;paint();if(connecting&&prefs.effective_channels[connecting])closeConnection();}
        catch(error){tell(error.message,true);}finally{setBusy(false);}
    }
    function connect(channel){
        connecting=channel;connection.hidden=false;connection.replaceChildren(element('h3','','Подключить '+CLIENT_CHANNEL_TITLES[channel]));cards[channel].card.append(connection);
        if(channel==='email'){
            connection.append(element('p','form-hint','Клиент получит письмо и подтвердит адрес по ссылке.'));
            const email=formField(connection,'Email клиента',prefs.email||'','email');email.maxLength=254;email.required=true;
            const send=button('Отправить подтверждение',async()=>{
                if(!email.reportValidity())return;
                try{await save({...selected('email',true),email:email.value});setBusy(true);const result=await post('/admin/api/client-notifications/verify-email',{token});const outcome=result.delivery||{};tell(['sent','accepted','queued','success'].includes(outcome.status)?'Письмо принято к отправке. Подтверждение выполняет клиент.':'Отправка не подтверждена. Проверьте настройки почты.',!['sent','accepted','queued','success'].includes(outcome.status));}
                catch(error){tell(error.message,true);}finally{setBusy(false);}
            });connection.append(send);
        }else if(channel==='telegram'||channel==='max'){
            connection.append(element('p','form-hint','Передайте ссылку клиенту: он должен открыть её в своём аккаунте мессенджера. Привязка не даёт административных прав.'));
            connection.append(button('Получить ссылку для клиента',async()=>{
                try{await save(selected(channel,true));setBusy(true);const result=await post('/admin/api/client-notifications/bot-link',{token,platform:channel});const box=element('div','link-box'),input=element('input');input.readOnly=true;input.value=result.link;input.setAttribute('aria-label','Ссылка для подключения клиента');box.append(input,iconButton('Копировать ссылку подключения','copy',()=>copyToClipboard(result.link)));connection.querySelector('.link-box')?.remove();connection.append(box);}
                catch(error){tell(error.message,true);}finally{setBusy(false);}
            }));
        }else if(channel==='sms')connection.append(element('p','form-hint','Добавьте корректный телефон в карточку клиента. SMS расходуют лимит филиала.'));
        else connection.append(element('p','form-hint',prefs.messengers?.reason==='yclients_client_missing'?'У клиента нет ID YCLIENTS. Для отправки по телефону подключите ChatPush в настройках филиала.':'Подключите выбранный сервис рассылки в настройках филиала.'));
        connection.append(button('Отмена',closeConnection));
    }
    function confirmUnsubscribe(){
        const prompt=createModal('Отключить уведомления клиента?');prompt.body.append(element('p','','Персональная ссылка и пропуск сохранятся. Возобновить рассылку можно включением нужного канала.'));
        const confirm=button('Отключить уведомления',async()=>{confirm.disabled=true;try{await save({enabled:false});closeConnection();prompt.close();tell('Уведомления отключены. Пропуск сохранён.');}catch(error){showAlert(error.message,'error');}finally{confirm.disabled=false;}},'btn-action btn-deny');prompt.actions.append(button('Отмена',prompt.close),confirm);
    }
    const refreshButton=button('Проверить подключения',refresh);view.actions.append(refreshButton,button('Закрыть',view.close));
    window.addEventListener('focus',refresh);await refresh();
}
