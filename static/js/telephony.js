'use strict';
(() => {
    const $ = id => document.getElementById(id);
    // Source-controlled vector paths only. Contact names and SMS always use textContent.
    const paths = {
        message:'M21 11.5a8.4 8.4 0 0 1-9 8.5 10 10 0 0 1-3.5-.7L3 21l1.6-5A8.2 8.2 0 0 1 3 11.5 8.4 8.4 0 0 1 12 3a8.4 8.4 0 0 1 9 8.5Z',
        phone:'M7.5 3H4.8C3.8 3 3 3.8 3 4.8A16.2 16.2 0 0 0 19.2 21c1 0 1.8-.8 1.8-1.8v-2.7l-5-2-1.9 1.9a13 13 0 0 1-6.5-6.5L9.5 8 7.5 3Z',
        compose:'M12 4H5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h13a2 2 0 0 0 2-2v-7M16 3l5 5M9 15l1-5L18 2l5 5-8 8-6 1Z',
        more:'M5 12h.01M12 12h.01M19 12h.01',search:'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z',
        back:'m14 5-7 7 7 7',up:'M12 19V5m-6 6 6-6 6 6','arrow-right':'M4 12h16m-6-6 6 6-6 6',
        close:'m6 6 12 12M6 18 18 6',trash:'M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7',
        user:'M20 21a8 8 0 0 0-16 0M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z',
        info:'M12 11v6m0-10h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0Z',
        copy:'M9 9h12v12H9ZM5 15H3V3h12v2',check:'m5 12 4 4L19 6',
        clock:'M12 6v6l4 2M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0Z',
        alert:'M12 8v5m0 4h.01M12 2 1 21h22L12 2Z',refresh:'M20 8a9 9 0 1 0 1 8M20 2v6h-6',
        incoming:'M18 6 6 18m0-9v9h9',outgoing:'M6 18 18 6m-9 0h9v9'
    };
    function icon(name) {
        const svg = document.createElementNS('http://www.w3.org/2000/svg','svg');
        for(const [key,value] of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':name==='more'?'3':'1.8','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true'})) svg.setAttribute(key,value);
        const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',paths[name] || paths.info);svg.append(path);return svg;
    }
    document.querySelectorAll('[data-icon]').forEach(node=>node.append(icon(node.dataset.icon)));
    const labels={received:'Получено',incomplete:'Получены не все части',queued:'В очереди',processing:'Отправляется',simulated:'Имитация — не отправлено',
        command_sent:'Команда вызова выполнена',network_accepted:'Принято сетью; доставка не подтверждена',partial:'Отправлено частично',unknown:'Результат неизвестен',failed:'Не отправлено',expired:'Истекло время в очереди',cancelled:'Отменено',
        ringing:'Входящий вызов',ended:'Завершён',pending:'Ожидает отправки',submitting:'Отправляется',
        sms_service_not_subscribed:'Сеть отклонила SMS: услуга не разрешена для SIM',sms_memory_full:'Память SMS заполнена',sms_rejected:'SMS отклонена модемом или сетью',sms_network_timeout:'Сеть не ответила в срок',sms_network_unavailable:'Сеть недоступна',sms_service_center_unknown:'Не настроен SMS-центр',sms_result_timeout:'Нет подтверждения результата',runtime_interrupted:'Работа приложения прерывалась',command_timeout:'Модем не ответил',
        home:'В сети',roaming:'Роуминг',searching:'Поиск сети',denied:'Регистрация отклонена',not_registered:'Нет сети',READY:'Готова'};
    const label=value=>labels[value] || value || '—';
    const fullDate=value=>value?new Date(value).toLocaleString('ru-RU'):'—';
    const time=value=>new Date(value).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'});
    function day(value) {
        const d=new Date(value), today=new Date(), yesterday=new Date();yesterday.setDate(today.getDate()-1);
        if(d.toDateString()===today.toDateString())return 'Сегодня';
        if(d.toDateString()===yesterday.toDateString())return 'Вчера';
        return d.toLocaleDateString('ru-RU',{day:'numeric',month:'long',...(d.getFullYear()!==today.getFullYear()?{year:'numeric'}:{})});
    }
    const shortDate=value=>day(value)==='Сегодня'?time(value):day(value)==='Вчера'?'Вчера':new Date(value).toLocaleDateString('ru-RU',{day:'numeric',month:'short'});
    const validPhone=value=>/^\+?[0-9 ()-]{7,40}$/.test(value || '');
    const requestId=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join('');
    const nameOf=contact=>contact.contact_name || contact.phone || 'Неизвестный номер';
    let mode='sms',selected=null,chatsPage=1,messagesPage=1,callsPage=1,revision=0,busy=false,refreshAgain=false;
    let chatsSignature='',messagesSignature='',callsSignature='',latestStatus={},lastError='',toastTimer;
    let smsSubmitting=false,callSubmitting=false,scrollToLatest=false,visibleMessages=[];
    const drafts=new Map(),expanded=new Set();
    function toast(text,error=false){clearTimeout(toastTimer);$('toast').textContent=text;$('toast').className='toast'+(error?' error':'');$('toast').hidden=false;toastTimer=setTimeout(()=>$('toast').hidden=true,5500);}
    function avatar(contact) {
        const node=element('div','avatar'),name=contact.contact_name || '',seed=name || contact.phone || '';
        node.style.setProperty('--avatar-hue',String([...seed].reduce((n,c)=>n+c.codePointAt(0),0)%360));
        if(name)node.textContent=name.trim().split(/\s+/).slice(0,2).map(s=>[...s][0]).join('').toLocaleUpperCase();else node.append(icon('user'));
        node.setAttribute('aria-hidden','true');return node;
    }
    function actionButton(title,name,callback,className='icon-button') {
        const node=button('',callback,className);node.title=title;node.setAttribute('aria-label',title);node.append(icon(name));return node;
    }
    function menu(anchor,items) {
        const box=$('contextMenu');box.hidePopover();box.replaceChildren();
        for(const item of items) {
            const control=button('',()=>{box.hidePopover();anchor.focus();item.run();},item.danger?'danger':'');
            control.setAttribute('role','menuitem');control.disabled=Boolean(item.disabled);
            if(item.id)control.id=item.id;
            control.append(icon(item.icon),element('span','',item.text));box.append(control);
        }
        const rect=anchor.getBoundingClientRect();box.showPopover();
        box.style.left=Math.max(8,Math.min(rect.right-box.offsetWidth,innerWidth-box.offsetWidth-8))+'px';
        box.style.top=Math.max(8,Math.min(rect.bottom+6,innerHeight-box.offsetHeight-8))+'px';
        box.querySelector('button:not(:disabled)')?.focus();
    }
    $('contextMenu').addEventListener('keydown',event=>{
        const nodes=[...$('contextMenu').querySelectorAll('button:not(:disabled)')],index=nodes.indexOf(document.activeElement);
        if(['ArrowUp','ArrowDown','Home','End'].includes(event.key)){event.preventDefault();nodes[event.key==='Home'?0:event.key==='End'?nodes.length-1:(index+(event.key==='ArrowDown'?1:-1)+nodes.length)%nodes.length]?.focus();}
    });
    function confirmRemoval(title,text) {
        $('confirmTitle').textContent=title;$('confirmText').textContent=text;
        const dialog=$('confirmDialog');dialog.returnValue='cancel';dialog.showModal();$('confirmCancel').focus();
        return new Promise(resolve=>dialog.addEventListener('close',()=>resolve(dialog.returnValue==='delete'),{once:true}));
    }
    $('confirmCancel').onclick=()=>$('confirmDialog').close('cancel');$('confirmDelete').onclick=()=>$('confirmDialog').close('delete');
    document.querySelectorAll('[data-close]').forEach(node=>node.addEventListener('click',()=>node.closest('dialog').close()));
    function details(title,rows,note='') {
        $('detailsTitle').textContent=title;const list=element('dl','detail-list');
        for(const [key,value] of rows){const row=element('div','detail-line');row.append(element('dt','',key),element('dd','',value ?? '—'));list.append(row);}
        $('detailsBody').replaceChildren(list);if(note)$('detailsBody').append(element('p','detail-note',note));
        $('detailsDialog').showModal();
    }
    function messageDetails(record) {
        const rows=[['Статус',label(record.status)],['Время SMS',fullDate(record.timestamp)]];
        if(record.code)rows.push(['Причина',label(record.code)]);
        if(record.total_parts)rows.push(['Частей',String(record.total_parts)]);
        for(const part of record.parts || []) {
            rows.push([`Часть ${part.sequence} · SMS-центр`,fullDate(part.smsc_at)]);
            if(part.received_at)rows.push(['Замечена приложением',fullDate(part.received_at)],['Записана в журнал',fullDate(part.archived_at)]);
        }
        for(const part of record.segments || [])rows.push([`Часть ${part.sequence}`,label(part.status)+(part.code?' · '+label(part.code):'')]);
        details('Сведения о сообщении',rows,record.direction==='outgoing'?'Принято сетью означает подтверждение SMS-центра. Доставка на телефон этим не подтверждается.':'Время SMS-центра и время приёма могут различаться. Для старых сообщений время приёма каждой части не записывалось.');
    }
    function recordText(record) {
        if(record.text != null)return record.text;
        const parts=new Map((record.parts || []).map(p=>[p.sequence,p.text ?? '[Двоичные данные]']));
        return Array.from({length:record.total_parts || 1},(_,i)=>parts.get(i+1) ?? `[…]`).join('');
    }
    async function deleteMessage(record) {
        if(!await confirmRemoval('Удалить сообщение?',record.status==='incomplete'?'Полученные части будут удалены. Ещё не пришедшие части могут появиться позднее.':'Оно будет удалено из журнала. Остальная переписка останется.'))return;
        try{await post(`/admin/api/telephony/sms/messages/${record.id}/delete`,{});messagesSignature='';chatsSignature='';revision++;await refresh();toast('Сообщение удалено');}
        catch(error){toast(error.message,true);}
    }
    const modemFilter=$('telephonyModem');
    const modemName=record=>record.modem_name || (record.modem_id===null?'':'SIM800C');
    function filteredUrl(path) {
        return modemFilter.value ? path + (path.includes('?')?'&':'?') + 'modem_id=' + encodeURIComponent(modemFilter.value) : path;
    }
    function messageCard(record) {
        const outgoing=record.direction==='outgoing',card=element('article','phone-message '+(outgoing?'phone-outgoing':'phone-incoming'));
        card.dataset.recordId=record.id;
        const row=element('div','bubble-row'),bubble=element('div','message-bubble'),text=recordText(record),long=text.length>550;
        const body=element('div','telephony-text'+(long&&!expanded.has(record.id)?' collapsed':''),text);bubble.append(body);
        if(long)bubble.append(button(expanded.has(record.id)?'Свернуть':'Показать полностью',event=>{
            expanded.has(record.id)?expanded.delete(record.id):expanded.add(record.id);
            body.classList.toggle('collapsed',!expanded.has(record.id));event.currentTarget.textContent=expanded.has(record.id)?'Свернуть':'Показать полностью';
        },'expand-message'));
        if(record.status==='incomplete'){
            const note=element('div','message-warning');note.append(icon('clock'),element('span','',`Получено ${record.received_parts || 0} из ${record.total_parts || '?'} частей`));bubble.append(note);
        }
        const actions=actionButton('Действия с сообщением','more',event=>menu(event.currentTarget,[
            {text:'Копировать',icon:'copy',run:async()=>toast(await copyToClipboard(text,false)?'Текст скопирован':'Не удалось скопировать текст')},
            {text:'Сведения',icon:'info',run:()=>messageDetails(record)},
            {text:'Удалить сообщение',icon:'trash',danger:true,disabled:['queued','processing'].includes(record.status),run:()=>deleteMessage(record)}
        ]),'icon-button message-action');actions.setAttribute('aria-haspopup','menu');row.append(bubble,actions);card.append(row);
        const meta=element('div','message-meta');meta.append(element('time','',time(record.timestamp)));meta.querySelector('time').dateTime=record.timestamp;
        if(modemName(record))meta.append(element('span','',modemName(record)));
        if(outgoing){
            const warning=['failed','partial','unknown','expired','cancelled'].includes(record.status);
            const state=element('span','message-status'+(warning?' status-error':''));state.title=label(record.status);state.setAttribute('aria-label',label(record.status));
            state.append(icon(warning?'alert':record.status==='network_accepted'?'check':'clock'));
            if(warning || record.status==='simulated')state.append(element('span','',record.status==='simulated'?'Имитация':label(record.status)));
            meta.append(state);
        }
        card.append(meta);return card;
    }
    function saveDraft(){if(selected)drafts.set(selected.id || selected.phone,$('smsForm').elements.text.value);}
    function updateComposer() {
        const area=$('smsForm').elements.text,text=area.value;let parts=text?1:0,units=0;
        if(text.length>70)for(const char of text){if(units+char.length>66){parts++;units=0;}units+=char.length;}
        const allowed=validPhone(selected?.phone);
        area.disabled=!allowed;area.placeholder=allowed?'Сообщение SMS':'На этого отправителя нельзя ответить';
        $('smsParts').textContent=text?`${text.length} симв. · ${parts} SMS${parts>255?' · слишком длинное сообщение':''}`:'SMS';
        $('smsParts').title='Каждая часть SMS тарифицируется оператором отдельно';
        $('smsForm').querySelector('[type=submit]').disabled=smsSubmitting || !allowed || !text.trim() || parts>255;
        area.style.height='auto';area.style.height=Math.min(144,Math.max(30,area.scrollHeight))+'px';
    }
    function threadHeading() {
        $('threadEmpty').hidden=Boolean(selected);$('threadContent').hidden=!selected;
        if(!selected)return;
        $('chatHeading').textContent=nameOf(selected);$('chatHeading').title=nameOf(selected);
        $('chatSubtitle').textContent=selected.contact_name?selected.phone || 'SMS':'SMS';
        const photo=avatar(selected);$('threadAvatar').replaceChildren(...photo.childNodes);$('threadAvatar').style.cssText=photo.style.cssText;
        $('callContact').disabled=!validPhone(selected.phone);$('threadMore').disabled=!selected.id;updateComposer();
    }
    async function selectChat(chat) {
        saveDraft();selected=chat;messagesPage=1;revision++;messagesSignature='';chatsSignature='';scrollToLatest=true;
        $('smsPhone').value=chat.phone || '';$('smsForm').elements.text.value=drafts.get(chat.id || chat.phone) || '';
        $('chatMessages').replaceChildren(element('p','list-empty','Загрузка…'));
        $('phoneShell').classList.add('thread-open');threadHeading();
        try{if(chat.id)await post(filteredUrl(`/admin/api/telephony/sms/chats/${chat.id}/read`),{});}
        catch(error){toast(error.message,true);}
        await refresh();
    }
    async function pages(path,count,payload) {
        const result=[];
        for(let page=1;page<=count;page++)result.push(payload?await post(path,{...payload,page,modem_id:modemFilter.value}):await api(filteredUrl(path+'?page='+page)));
        return result;
    }
    async function refreshSms(token) {
        const results=await pages('/admin/api/telephony/sms/chats',chatsPage,{search:$('smsSearch').value});
        if(token!==revision)return;
        const chats=results.flatMap(r=>r.chats),signature=JSON.stringify([chats,selected?.id]);
        if(signature!==chatsSignature){
            chatsSignature=signature;$('chatList').replaceChildren();
            for(const chat of chats){
                const control=button('',()=>selectChat(chat),'phone-chat'+(selected?.id===chat.id?' phone-selected':''));control.setAttribute('aria-pressed',String(selected?.id===chat.id));
                const copy=element('div','chat-copy'),first=element('div','chat-first-line');first.append(element('span','chat-name',nameOf(chat)),element('time','chat-time',shortDate(chat.timestamp)));
                copy.append(first,element('span','phone-preview',(chat.direction==='outgoing'?'Вы: ':'')+chat.preview));control.append(avatar(chat),copy);
                if(chat.unread){const dot=element('span','unread-dot');dot.setAttribute('aria-label',`Непрочитанных: ${chat.unread}`);control.append(dot);}
                $('chatList').append(control);
            }
            if(!chats.length)$('chatList').append(element('p','list-empty',$('smsSearch').value?'Ничего не найдено':'Здесь появятся ваши диалоги'));
        }
        $('loadChats').hidden=chats.length>=results[0].total;
        if(!selected){threadHeading();return;}
        let messages=[],total=0;
        if(selected.id){
            const key=selected.id,responses=await pages(`/admin/api/telephony/sms/chats/${key}`,messagesPage);
            if(token!==revision || selected?.id!==key)return;
            selected={...selected,contact_name:responses[0].contact_name};messages=responses.flatMap(r=>r.messages);total=responses[0].total;
            if(messages.some(r=>r.direction==='incoming'&&!r.read) && !document.hidden && $('phoneShell').classList.contains('thread-open'))await post(filteredUrl(`/admin/api/telephony/sms/chats/${key}/read`),{});
            if(token!==revision)return;
        }
        threadHeading();$('loadMessages').hidden=messages.length>=total;
        const next=JSON.stringify([selected.id,selected.phone,messages]);visibleMessages=messages;
        if(next!==messagesSignature){
            const scroller=$('messageScroll'),bottom=scroller.scrollHeight-scroller.scrollTop-scroller.clientHeight,wasAtBottom=bottom<70;
            messagesSignature=next;$('chatMessages').replaceChildren();let previousDay='';
            for(const record of messages.slice().reverse()){
                const title=day(record.timestamp);if(title!==previousDay){$('chatMessages').append(element('div','day-divider',title));previousDay=title;}
                $('chatMessages').append(messageCard(record));
            }
            if(!messages.length)$('chatMessages').append(element('p','list-empty','Начните переписку'));
            scroller.scrollTop=scrollToLatest || wasAtBottom?scroller.scrollHeight:scroller.scrollHeight-scroller.clientHeight-bottom;
            scrollToLatest=false;
        }
    }
    async function refreshCalls(token) {
        const results=await pages('/admin/api/telephony/records',callsPage,{kind:'call',direction:$('callDirection').value,search:$('smsSearch').value});
        if(token!==revision)return;
        const records=results.flatMap(r=>r.records),signature=JSON.stringify(records);
        if(signature!==callsSignature){
            callsSignature=signature;$('callList').replaceChildren();
            for(const record of records){
                const row=element('article','call-row');row.tabIndex=0;row.setAttribute('role','button');row.setAttribute('aria-label',`${nameOf(record)}. ${label(record.status)}. ${fullDate(record.timestamp)}`);
                const copy=element('div','chat-copy'),first=element('div','chat-first-line'),description=element('div','call-description');
                first.append(element('span','chat-name',nameOf(record)),element('time','chat-time',shortDate(record.timestamp)));
                description.append(icon(record.direction),element('span','',record.direction==='incoming'?'Входящий':'Исходящий'));
                if(modemName(record))description.append(element('span','','· '+modemName(record)));
                if(['failed','unknown','cancelled','expired'].includes(record.status))description.classList.add('status-error');
                copy.append(first,description);row.append(avatar(record),copy);
                const open=()=>{openDial(record.phone,record.contact_name);$('callResult').textContent=fullDate(record.timestamp)+' · '+label(record.status);};
                row.addEventListener('click',open);row.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();open();}});
                $('callList').append(row);
            }
            if(!records.length)$('callList').append(element('p','list-empty','Звонков пока нет'));
        }
        $('loadCalls').hidden=records.length>=results[0].total;
    }
    function displayedModem(status) {
        if(!modemFilter.value)return status.modem || {};
        return status.modems?.find(m=>m.id===modemFilter.value)?.status || {};
    }
    function modemStatus(status) {
        latestStatus=status;
        const names=new Map(Object.entries(status.archived_modems || {}));
        for(const device of status.modems || [])names.set(device.id,device.name);
        const signature=JSON.stringify([...names]);
        if(modemFilter.dataset.options!==signature){
            const selectedId=modemFilter.value;modemFilter.dataset.options=signature;
            modemFilter.replaceChildren(new Option('Все модемы',''));
            for(const [key,name] of names)modemFilter.add(new Option(name,key));
            modemFilter.value=names.has(selectedId)?selectedId:'';
        }
        const modem=displayedModem(status),sms=modem.sms_poll || {};
        const stale=modem.checked_at && Date.now()-new Date(modem.checked_at).getTime()>90000;
        const online=modem.state==='ok'&&['home','roaming'].includes(modem.registration)&&!stale;
        let text=!status.enabled?'Модем выключен':online?(modem.operator||'В сети')+(modem.registration==='roaming'?' · роуминг':''):stale?'Нет свежих данных':modem.state==='error'?'Модем недоступен':modem.checked_at?'Поиск сети':'Подключение…';
        if(modemFilter.value && !status.modems?.some(m=>m.id===modemFilter.value))text='Модем в архиве';
        else if(status.stopping)text='Остановка';else if(online&&sms.state==='partial')text='Проверить приём SMS';
        $('connectionLabel').textContent=text;$('modemStatus').className='connection'+(modem.state==='error'?' error':(!online||sms.state==='partial'?' warning':''));
        const bars=online&&modem.rssi!=null?Math.max(1,Math.min(4,Math.ceil(modem.rssi/8))):0;
        $('modemStatus').querySelectorAll('i').forEach((node,i)=>node.classList.toggle('lit',i<bars));
        $('modeBadge').hidden=!status.dry_run;
    }
    async function refresh() {
        if(busy){refreshAgain=true;return;}busy=true;const token=revision;refreshAgain=false;
        try{modemStatus(await api('/admin/api/telephony/status'));if(mode==='sms')await refreshSms(token);else await refreshCalls(token);lastError='';}
        catch(error){if(error.message!==lastError)toast(error.message,true);lastError=error.message;}
        finally{busy=false;if(token!==revision || refreshAgain)setTimeout(refresh,0);}
    }
    function setMode(next) {
        mode=next;revision++;$('smsPanel').hidden=next!=='sms';$('callsPanel').hidden=next!=='calls';$('chatList').hidden=next!=='sms';$('callList').hidden=next!=='calls';$('callFilters').hidden=next!=='calls';$('loadChats').hidden=true;$('loadCalls').hidden=true;
        $('sectionTitle').textContent=next==='sms'?'Сообщения':'Звонки';$('smsSearch').value='';chatsPage=callsPage=1;
        $('phoneShell').classList.remove('thread-open');
        for(const [id,value] of [['modeSms','sms'],['modeCalls','calls']]){$(id).setAttribute('aria-selected',String(value===next));$(id).tabIndex=value===next?0:-1;}
        $('newChatOpen').setAttribute('aria-label',next==='sms'?'Новое сообщение':'Новый вызов');$('newChatOpen').title=next==='sms'?'Новое сообщение':'Новый вызов';refresh();
    }
    $('modeSms').onclick=()=>setMode('sms');$('modeCalls').onclick=()=>setMode('calls');
    document.querySelector('.phone-tabs').addEventListener('keydown',event=>{if(['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();setMode(mode==='sms'?'calls':'sms');$(mode==='sms'?'modeSms':'modeCalls').focus();}});
    function openDial(phone='',name='') {
        if(mode!=='calls')setMode('calls');$('callPhone').value=validPhone(phone)?phone:'';$('callContactName').textContent=name || 'Введите номер с кодом страны';$('callResult').textContent='';$('phoneShell').classList.add('thread-open');$('callPhone').focus();
    }
    function newMessage(){if(mode==='calls'){openDial();return;}$('newChatDialog').showModal();$('newChatPhone').focus();}
    $('newChatOpen').onclick=newMessage;$('emptyCompose').onclick=newMessage;
    $('newChatForm').addEventListener('submit',async event=>{
        event.preventDefault();const phone=$('newChatPhone').value.trim();
        if(!validPhone(phone)){$('newChatPhone').setCustomValidity('Введите номер с кодом страны');$('newChatPhone').reportValidity();return;}
        const submit=event.currentTarget.querySelector('[type=submit]');submit.disabled=true;
        try{const contact=await post('/admin/api/telephony/contacts/resolve',{phone});$('newChatDialog').close();await selectChat(contact);$('smsForm').elements.text.focus();}
        catch(error){toast(error.message,true);}finally{submit.disabled=false;}
    });
    $('newChatPhone').oninput=()=>{$('newChatPhone').setCustomValidity('');};
    for(const id of ['backToChats','backToCalls'])$(id).onclick=()=>{$('phoneShell').classList.remove('thread-open');$(mode==='sms'?'modeSms':'modeCalls').focus();};
    $('callContact').onclick=()=>openDial(selected?.phone,selected?.contact_name);
    let searchTimer;$('smsSearch').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{chatsPage=callsPage=1;revision++;refresh();},250);};
    $('callDirection').onchange=()=>{callsPage=1;revision++;refresh();};
    modemFilter.onchange=()=>{
        saveDraft();selected=null;chatsPage=callsPage=messagesPage=1;chatsSignature=callsSignature=messagesSignature='';
        threadHeading();$('phoneShell').classList.remove('thread-open');revision++;refresh();
    };
    $('loadChats').onclick=()=>{chatsPage++;revision++;refresh();};$('loadCalls').onclick=()=>{callsPage++;revision++;refresh();};$('loadMessages').onclick=()=>{messagesPage++;revision++;refresh();};
    async function clearSms(all=false) {
        const target=selected?.id;if(!all&&!target)return;
        const path=filteredUrl(all?'/admin/api/telephony/sms/clear':`/admin/api/telephony/sms/chats/${target}/clear`);
        const scope=modemFilter.value?'Модем: '+modemFilter.selectedOptions[0].text+'. ':'';
        if(!await confirmRemoval(all?'Удалить всю переписку?':'Очистить чат?',scope+(all?'Все SMS будут удалены из журнала. История звонков останется.':'Все сообщения этого чата будут удалены из журнала.')))return;
        try{await post(path,{});if(all || selected?.id===target){saveDraft();selected=null;threadHeading();$('phoneShell').classList.remove('thread-open');}chatsPage=1;messagesSignature=chatsSignature='';revision++;await refresh();toast('Переписка удалена');}
        catch(error){toast(error.message,true);}
    }
    $('sidebarMore').onclick=event=>menu(event.currentTarget,[{text:'Обновить',icon:'refresh',run:refresh},...(mode==='sms'?[{id:'clearAllSms',text:'Удалить все сообщения',icon:'trash',danger:true,run:()=>clearSms(true)}]:[])]);
    $('threadMore').onclick=event=>menu(event.currentTarget,[{text:'Скопировать номер',icon:'copy',run:async()=>toast(await copyToClipboard(selected.phone,false)?'Номер скопирован':'Не удалось скопировать номер')},{id:'clearChat',text:'Очистить чат',icon:'trash',danger:true,run:()=>clearSms()}]);
    $('modemStatus').onclick=()=>{
        const m=displayedModem(latestStatus),sms=m.sms_poll || {};
        details('Состояние связи',[['Модем',latestStatus.enabled?'Включён':'Выключен'],['Режим',latestStatus.dry_run?'Имитация':'Реальные вызовы и SMS'],['SIM',label(m.sim)],['Оператор',m.operator||'Не определён'],['Сеть',label(m.registration)],['Сигнал',m.signal_dbm!=null?m.signal_dbm+' dBm':'—'],['Обновлено',fullDate(m.checked_at)],['Ожидают отправки',latestStatus.queue_length || 0],['Приём SMS',sms.state==='ok'?'Работает':sms.state==='partial'?'Есть ошибки':'Ожидание проверки'],['Ошибки чтения',sms.parse_errors || 0],['Ошибки очистки',sms.cleanup_errors || 0]],m.error_code?label(m.error_code):'Входящие SMS сохраняются в журнал перед удалением с SIM.');
    };
    $('smsForm').elements.text.addEventListener('input',()=>{saveDraft();updateComposer();});
    $('smsForm').elements.text.addEventListener('keydown',event=>{if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)){event.preventDefault();if(!$('smsForm').querySelector('[type=submit]').disabled)$('smsForm').requestSubmit();}});
    for(const kind of ['sms','call']) {
        const form=$(kind+'Form');let pending=null;
        form.addEventListener('submit',async event=>{
            event.preventDefault();if(kind==='sms'?smsSubmitting:callSubmitting)return;
            if(kind==='sms')smsSubmitting=true;else callSubmitting=true;
            const submit=form.querySelector('[type=submit]');submit.disabled=true;
            const fields=Object.fromEntries(new FormData(form)),payload={kind,...fields},draftKey=selected?.id || selected?.phone;
            try{
                if(kind==='call')payload.duration=Number(fields.duration);else await post('/admin/api/telephony/preview',fields);
                const signature=JSON.stringify(payload);if(!pending||pending.signature!==signature)pending={signature,id:requestId()};
                const result=await post('/admin/api/telephony/commands',{...payload,request_id:pending.id});pending=null;
                if(kind==='sms'){
                    drafts.delete(draftKey);
                    // Don't overwrite another chat or text typed while this request was in flight.
                    if((selected?.id || selected?.phone)===draftKey){
                        selected={...selected,id:result.chat_id};messagesPage=1;scrollToLatest=true;
                        if(form.elements.text.value===fields.text)form.elements.text.value='';saveDraft();revision++;messagesSignature=chatsSignature='';
                    }
                    if(result.delivery==='simulated')toast('Имитация — SMS не отправлено');
                }else{$('callResult').textContent=label(result.delivery);toast(label(result.delivery));}
                await refresh();
            }catch(error){toast(error.message,true);}
            finally{if(kind==='sms'){smsSubmitting=false;updateComposer();}else{callSubmitting=false;submit.disabled=false;}}
        });
    }
    document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
    refresh();setInterval(()=>{if(!document.hidden)refresh();},2000);
})();
