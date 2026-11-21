'use strict';
(() => {
    const config=JSON.parse(document.getElementById('accessConfig').textContent);
    const $=id=>document.getElementById(id);
    let active=true,pending=false,deadline=0,expires=0,statusBusy=false,buttonSignature='',connectionError=false;
    let hours=config.access_hours||{open:true};
    const text=(node,value)=>{node.textContent=value;};
    async function request(path,payload){
        let response;try{response=await fetch(path,{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},body:JSON.stringify(payload)});}catch(error){window.GateBotPWA?.networkLost();throw error;}
        let data;try{data=await response.json();}catch{throw new Error('Нет ответа сервера');}
        return {response,data};
    }
    function applyButtons(buttons){
        const gates=(buttons || []).filter(b=>b.type==='gate'),signature=JSON.stringify(gates);
        if(signature!==buttonSignature){buttonSignature=signature;$('gateActions').replaceChildren();for(const item of gates){const control=document.createElement('button');control.type='button';control.className='gate-action button-gate';control.dataset.buttonId=item.id;const title=document.createElement('span');title.textContent=item.label;const arrow=document.createElement('span');arrow.textContent='↗';arrow.setAttribute('aria-hidden','true');control.append(title,arrow);control.onclick=()=>openGate(item.id);$('gateActions').append(control);}}
        $('adminContacts').replaceChildren();
        const contacts=(buttons||[]).filter(b=>b.type==='admin');
        for(const contact of contacts){
            if(!/^\+?[0-9 ()-]+$/.test(contact.phone||''))continue;
            const link=document.createElement('a');
            link.className='btn-primary button-admin';link.href='tel:'+contact.phone;
            link.textContent='Позвонить администратору'+(contacts.length>1?' · '+contact.label:'');link.dataset.deskPhone=contact.id;
            $('adminContacts').append(link);
        }
    }
    function render(){
        if(expires&&Date.now()>=expires)active=false;
        if(hours.next_change_at&&Date.now()>=Date.parse(hours.next_change_at)){hours={...hours,open:false,next_change_at:null};refresh();}
        const closed=hours.open===false;
        const disconnected=navigator.onLine===false||window.GateBotPWA?.online===false;
        $('gateActions').hidden=!active||disconnected;
        $('accessBadge').textContent=closed?'Вне рабочих часов':active?'Действует':'Неактивен';$('accessBadge').classList.toggle('active',active&&!closed);
        $('accessHeading').textContent=closed?'Сейчас доступ закрыт':active?'Добро пожаловать':'Пропуск сейчас неактивен';
        $('accessDescription').textContent=closed?(hours.message||'Извините, доступ возможен только в рабочие часы. Вы можете связаться с администратором.'):active?'Откройте нужные ворота, когда будете рядом.':'За уточнениями обратитесь к администратору филиала.';
        const seconds=Math.max(0,Math.ceil((deadline-performance.now())/1000));
        document.querySelectorAll('[data-button-id]').forEach(button=>button.disabled=pending||!active||closed||seconds>0||disconnected);
        $('gateCooldown').hidden=!active||seconds===0;$('gateCooldown').textContent=seconds?`Следующее открытие через ${seconds} с`:'';
    }
    function applyState(state){
        active=state.active===true;
        hours=state.access_hours||{open:true};
        if(hours.next_change_at&&state.server_time){const offset=Date.now()-new Date(state.server_time).getTime();hours={...hours,next_change_at:new Date(Date.parse(hours.next_change_at)+offset).toISOString()};}
        if(state.expires_at){const server=state.server_time?new Date(state.server_time).getTime():Date.now();expires=Date.now()+new Date(state.expires_at).getTime()-server;}else expires=0;
        if(Number(state.retry_after)>0)deadline=Math.max(deadline,performance.now()+Number(state.retry_after)*1000);
        if(Array.isArray(state.buttons))applyButtons(state.buttons);
        for(const [key,id] of [['preferences_url','preferencesLink'],['telegram_url','telegramLink'],['max_url','maxLink']]){const allowed=Boolean(state[key])&&(key!=='preferences_url'||!(state.is_temporary??config.is_temporary));$(id).hidden=!allowed;if(allowed)$(id).href=key==='preferences_url'&&window.GateBotPWA?.installed&&window.GateBotPWA.preferencesUrl?window.GateBotPWA.preferencesUrl:state[key];}
        render();
    }
    async function refresh(){
        if(statusBusy||navigator.onLine===false||window.GateBotPWA?.online===false)return;statusBusy=true;
        try{const {response,data}=await request('/api/access/status',{token:config.token});if(response.ok){if(connectionError){$('statusMessage').textContent='';connectionError=false;}applyState(data);}else{active=false;render();}}
        catch{connectionError=true;active=false;render();$('statusMessage').textContent='Не удалось обновить пропуск. Проверяем соединение…';$('statusMessage').className='status-message error';}
        finally{statusBusy=false;}
    }
    async function openGate(buttonId){
        if(pending||!active||hours.open===false||performance.now()<deadline||navigator.onLine===false||window.GateBotPWA?.online===false)return;
        pending=true;deadline=performance.now()+Math.max(20,Number(config.cooldown)||20)*1000;render();text($('statusMessage'),'Отправляем команду…');$('statusMessage').className='status-message';
        try{
            const requestId=Array.from(crypto.getRandomValues(new Uint8Array(16)),byte=>byte.toString(16).padStart(2,'0')).join('');
            const {response,data}=await request('/open-gate',{token:config.token,button_id:buttonId,request_id:requestId});const ok=response.ok&&data.status==='success';
            text($('statusMessage'),data.message||(ok?'Команда принята':'Не удалось отправить команду'));$('statusMessage').className='status-message '+(ok?'success':'error');
            if(!ok&&Number(data.retry_after)>0)deadline=Math.max(deadline,performance.now()+Number(data.retry_after)*1000);
            if(ok)document.dispatchEvent(new CustomEvent('gatebot:gate-command',{detail:requestId}));
            if(!ok&&[403,503].includes(response.status)&&!Number(data.retry_after))deadline=0;
            if(data.code==='outside_working_hours')hours={open:false,message:data.message};
            else if(!ok&&[401,403,404].includes(response.status))active=false;
        }catch{text($('statusMessage'),'Нет подтверждения сервера. Подождите перед повторным нажатием.');$('statusMessage').className='status-message error';}
        finally{pending=false;render();refresh();}
    }
    applyButtons(config.buttons);expires=config.expires_at?new Date(config.expires_at).getTime():0;render();refresh();setInterval(render,250);setInterval(()=>{if(!document.hidden)refresh();},5000);
    document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
})();
