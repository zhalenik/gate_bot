'use strict';
(() => {
    const $=id=>document.getElementById(id),list=$('notificationList');
    if(!list)return;
    let items=[],unreadOnly=false,busy=false;
    const element=(tag,cls,text)=>{const node=document.createElement(tag);node.className=cls;if(text!==undefined)node.textContent=text;return node;};
    const titles={gate_requested:'Запрос открытия ворот',gate_denied:'Отказ в доступе',gate_error:'Ошибка открытия',admin_contact_requested:'Запрос контакта администратора',invitation_accepted:'Приглашение принято',access_changed:'Пропуск изменён',crm_error:'Связь с CRM',crm_recovered:'CRM снова доступна',modem_error:'Модем недоступен',modem_recovered:'Модем снова доступен',delivery_failed:'Сообщение не доставлено',subscription_expiring:'Подписка заканчивается',subscription_expired:'Подписка закончилась',quota_warning:'Квота заканчивается',quota_exhausted:'Квота исчерпана',branch_registered:'Регистрация нового филиала',marketplace_disconnected:'Отключение в маркетплейсе YCLIENTS',sms_received:'Входящее SMS',call_received:'Входящий звонок'};
    function icon(kind){
        const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('width','20');svg.setAttribute('height','20');svg.setAttribute('fill','none');svg.setAttribute('stroke','currentColor');svg.setAttribute('stroke-width','1.7');svg.setAttribute('stroke-linecap','round');svg.setAttribute('stroke-linejoin','round');svg.setAttribute('aria-hidden','true');
        const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',kind==='read'?'m5 12 4 4L19 6':kind.includes('sms')||kind.includes('delivery')?'M4 4h16v12H9l-5 4V4m4 4h8m-8 4h5':kind.includes('gate')?'M5 20V9h5v11M7 9l12-5M16 20v-8m-3 3 3-3 3 3':kind.includes('error')||kind.includes('exhausted')?'M12 8v5m0 4h.01M12 3 2 21h20L12 3':'M12 3a6 6 0 0 0-6 6v5l-2 3h16l-2-3V9a6 6 0 0 0-6-6m-2 17h4');svg.append(path);return svg;
    }
    function fail(text){$('notificationError').textContent=text;$('notificationError').hidden=false;}
    function render(){
        const count=items.filter(item=>!item.read).length;$('notificationCount').textContent=count;$('notificationCount').hidden=!count;
        $('showAllNotifications').setAttribute('aria-pressed',String(!unreadOnly));$('showUnreadNotifications').setAttribute('aria-pressed',String(unreadOnly));
        list.replaceChildren();const visible=items.filter(item=>!unreadOnly||!item.read).sort((a,b)=>b.created-a.created);
        if(!visible.length){const empty=element('div','empty-state');empty.append(element('h2','',unreadOnly?'Всё прочитано':'Пока нет уведомлений'),element('p','',unreadOnly?'Новые события появятся здесь.':'Здесь будут запросы, изменения доступа и служебные события.'));list.append(empty);return;}
        for(const item of visible){
            const article=element('article','notification-item'+(item.read?'':' is-unread')),mark=element('span','notification-icon'),content=element('div','notification-content'),heading=element('div','notification-heading');mark.append(icon(item.event||''));
            const lines=String(item.text||'').split('\n'),title=lines.shift()||titles[item.event]||'Событие';heading.append(element('h2','',title));
            const timestamp=new Date(Number(item.created)*1000),time=element('time','',Number.isNaN(timestamp.getTime())?'':timestamp.toLocaleString('ru-RU',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}));if(!Number.isNaN(timestamp.getTime()))time.dateTime=timestamp.toISOString();heading.append(time);content.append(heading);
            if(lines.length)content.append(element('p','notification-text',lines.join('\n')));
            if(item.thread_id&&['pass_requested','client_message'].includes(item.event)){
                const link=element('a','btn-action','Перейти к диалогу');
                link.href=(location.pathname.startsWith('/office/')?'/office/messages':'/admin/client-messages')+'?thread='+encodeURIComponent(item.thread_id);content.append(link);
                if(item.event==='pass_requested'&&item.desk_message_id){
                    const allow=element('button','btn-action','Разрешить до конца дня');allow.type='button';content.append(allow);
                    allow.onclick=async()=>{
                        allow.disabled=true;
                        try{
                            const url='/admin/api/desk/threads/'+encodeURIComponent(item.thread_id)+'/approve/'+encodeURIComponent(item.desk_message_id);
                            const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},body:'{}'});
                            if(!response.ok)throw Error((await response.json()).message||'Не удалось разрешить пропуск');
                            allow.textContent='Пропуск разрешён';
                        }catch(error){fail(error.message);allow.disabled=false;}
                    };
                }
            }
            article.append(mark,content);
            if(!item.read){const button=element('button','icon-button notification-read');button.type='button';button.title='Отметить прочитанным';button.setAttribute('aria-label','Отметить прочитанным: '+title);button.append(icon('read'));button.onclick=async()=>{button.disabled=true;try{const response=await fetch('/admin/api/notification-inbox/'+encodeURIComponent(item.id)+'/read',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},body:'{}'});if(!response.ok)throw new Error('Не удалось обновить уведомление');item.read=true;render();}catch(error){fail(error.message);button.disabled=false;}};article.append(button);}
            list.append(article);
        }
    }
    async function refresh(){if(busy)return;busy=true;list.setAttribute('aria-busy','true');$('refreshNotifications').disabled=true;try{const response=await fetch('/admin/api/notification-inbox',{credentials:'same-origin'});const value=await response.json();if(!response.ok)throw new Error(value.message||'Не удалось загрузить уведомления');items=value.items||[];$('notificationError').hidden=true;render();}catch(error){fail(error.message);}finally{busy=false;list.setAttribute('aria-busy','false');$('refreshNotifications').disabled=false;}}
    $('showAllNotifications').onclick=()=>{unreadOnly=false;render();};$('showUnreadNotifications').onclick=()=>{unreadOnly=true;render();};$('refreshNotifications').onclick=refresh;
    refresh();setInterval(()=>{if(!document.hidden)refresh();},30000);
})();
