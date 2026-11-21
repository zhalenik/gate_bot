'use strict';
(() => {
    const client=document.querySelector('[data-client-desk]');
    const admin=document.querySelector('[data-admin-desk]');
    const root=client||admin;
    if(!root)return;
    const get=name=>root.querySelector('[data-desk-'+name+']');
    const status=text=>{get('status').textContent=text;};
    const newKey=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),byte=>byte.toString(16).padStart(2,'0')).join('');
    const api=async(path,payload)=>{
        const options={credentials:'same-origin',cache:'no-store'};
        if(payload)Object.assign(options,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},body:JSON.stringify(payload)});
        const response=await fetch(path,options);
        if(!response.headers.get('content-type')?.includes('application/json'))throw Error('Обновите страницу и войдите снова.');
        const result=await response.json();
        if(!response.ok)throw Error(result.message||'Не удалось выполнить действие.');
        return result;
    };
    const button=(label,fn)=>{
        const el=document.createElement('button');el.type='button';el.className='btn-action';el.textContent=label;
        el.onclick=()=>perform(el,fn);return el;
    };
    async function perform(control,fn){
        control.disabled=true;status('');
        try{await fn();}catch(error){status(error.message||'Ошибка соединения. Повторите попытку.');}
        finally{control.disabled=false;}
    }
    const history=get('history');
    function showMessages(messages,approve){
        const atBottom=history.scrollHeight-history.scrollTop-history.clientHeight<20;
        history.replaceChildren();
        for(const item of messages){
            const row=document.createElement('li'), text=document.createElement('p'), time=document.createElement('small');
            const mine=item.sender===(client?'client':'admin');
            row.classList.toggle('desk-own',mine);text.textContent=item.body;
            time.textContent=(mine?'Вы':client?'Администратор':'Клиент')+' · '+new Date(item.created*1000).toLocaleString('ru-RU',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});
            row.append(time,text);
            if(item.kind==='request'){
                if(item.approved){const label=document.createElement('small');label.textContent='Пропуск разрешён';row.append(label);}
                else if(approve)row.append(button('Выдать разовый пропуск',()=>approve(item.id)));
            }
            history.append(row);
        }
        if(atBottom)history.scrollTop=history.scrollHeight;
    }
    function compose(send){
        const form=get('compose'), field=form.querySelector('textarea'), submit=form.querySelector('button');
        let key=null,lastText='';
        form.onsubmit=event=>{
            event.preventDefault();
            const text=field.value.trim();if(!text)return;
            if(text!==lastText||!key){key=newKey();lastText=text;}
            perform(submit,async()=>{await send(text,key);if(field.value.trim()===text)field.value='';key=null;});
        };
    }
    if(client){
        const base='/api/pass/'+encodeURIComponent(client.dataset.clientDesk);
        const dialog=get('dialog'), shareForm=get('share-form'), linkBox=get('link'), input=linkBox.querySelector('input');
        const noPhone=get('no-phone'), phone=get('guest-phone'), deliver=get('deliver');
        let canDeliver=false, shareKey=null;
        function shareOptions(){
            phone.disabled=noPhone.checked;phone.required=!noPhone.checked;
            get('send-option').hidden=noPhone.checked||!canDeliver||phone.value.replace(/\D/g,'').length<7;
            if(get('send-option').hidden)deliver.checked=false;
        }
        const showClient=view=>{
            showMessages(view.messages);get('request').hidden=view.active||view.messages.some(m=>m.kind==='request'&&!m.approved);
            if(!get('share')&&view.active&&!root.querySelector('[data-entry-link]')){const back=button('Перейти к воротам',()=>location.reload());get('actions').prepend(back);back.dataset.entryLink='';}
        };
        const refresh=async()=>{
            const view=await api(base+'/desk');
            if(root.querySelector('[data-entry-link]'))root.querySelector('[data-entry-link]').remove();
            if(!shareForm.hidden){
                canDeliver=view.share_delivery;shareOptions();
                shareForm.querySelector('button[type=submit]').disabled=!view.active;
                if(!view.active)status('Для передачи ссылки нужен действующий пропуск.');
            }else showClient(view);
        };
        function open(screen){
            get('actions').hidden=screen!=='actions';get('chat').hidden=screen!=='chat';shareForm.hidden=screen!=='share';
            if(screen==='share'){shareForm.reset();linkBox.hidden=true;shareKey=newKey();shareOptions();}
            root.querySelector('#desk-title').textContent=screen==='share'?'Поделиться ссылкой':screen==='chat'?'Чат с администратором':'Обратиться к администратору';
            status('');if(!dialog.open)dialog.showModal();refresh().catch(err=>status(err.message));
        }
        get('open').onclick=()=>open('actions');
        get('open-chat').onclick=()=>open('chat');
        if(location.hash==='#messages')open('chat');
        if(get('share'))get('share').onclick=()=>open('share');
        get('close').onclick=()=>dialog.close();
        compose(async(body,key)=>showClient(await api(base+'/desk',{body,key,kind:'message'})));
        get('request').onclick=()=>perform(get('request'),async()=>{const view=await api(base+'/desk',{kind:'request',key:newKey()});showClient(view);status(view.active?'Разовый пропуск выдан.':'Запрос отправлен.');});
        get('actions').addEventListener('click',event=>{
            const contact=event.target.closest('a[data-desk-phone]');
            if(contact)fetch('/api/access/contact',{method:'POST',keepalive:true,credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},body:JSON.stringify({token:client.dataset.clientDesk,button_id:contact.dataset.deskPhone})}).catch(()=>{});
        });
        shareForm.addEventListener('input',event=>{if(event.target!==input){shareKey=newKey();linkBox.hidden=true;shareOptions();}});
        shareForm.onsubmit=event=>{
            event.preventDefault();
            perform(shareForm.querySelector('button'),async()=>{
                const result=await api(base+'/share',{phone:noPhone.checked?null:phone.value,no_phone:noPhone.checked,send:deliver.checked,key:shareKey});
                input.value=new URL(result.path,location.origin).href;
                linkBox.hidden=false;
                status(result.delivery==='not_requested'?'':(['accepted','sent','success'].includes(result.delivery)?'Ссылка отправлена гостю.':'Сервис не подтвердил отправку. Скопируйте ссылку для гостя.'));
            });
        };
        get('send-link').onclick=()=>perform(get('send-link'),async()=>{
            if(navigator.clipboard){await navigator.clipboard.writeText(input.value);status('Ссылка скопирована.');}
            else{input.focus();input.select();status('Скопируйте ссылку.');}
        });
        setInterval(()=>{if(dialog.open&&!document.hidden&&shareForm.hidden)refresh().catch(err=>status(err.message));},10000);
    }else{
        const base='/admin/api/desk/threads';
        let selected=Number(new URLSearchParams(location.search).get('thread'))||0, threads=[], visit=0;
        function showThread(view){
            if(selected!==view.id)return;
            get('compose').hidden=false;get('name').textContent=view.name;
            showMessages(view.messages,async id=>{showThread(await api(base+'/'+view.id+'/approve/'+id,{}));await refresh();});
        }
        function list(){
            const target=get('threads'), search=get('search').value.trim().toLocaleLowerCase();target.replaceChildren();
            for(const t of threads.filter(t=>t.name.toLocaleLowerCase().includes(search))){
                const ctl=button(t.name,async()=>{
                    selected=t.id;visit++;get('compose').querySelector('textarea').value='';get('compose').hidden=true;
                    history.replaceChildren();get('name').textContent=t.name;list();showThread(await api(base+'/'+t.id));
                });
                ctl.setAttribute('aria-pressed',String(selected===t.id));
                const sub=document.createElement('small');sub.textContent=t.pending?'Запрос пропуска':new Date(t.updated*1000).toLocaleDateString('ru-RU');ctl.append(sub);target.append(ctl);
            }
            if(!target.children.length)target.textContent=search?'Клиент не найден':'Обращений пока нет';
        }
        async function refresh(){
            const version=++visit,result=await api(base);if(version!==visit)return;
            threads=result.items;list();
            if(selected&&threads.some(t=>t.id===selected))showThread(await api(base+'/'+selected));
            else{selected=0;get('compose').hidden=true;get('name').textContent='Выберите диалог';history.replaceChildren();}
        }
        get('search').oninput=list;
        compose(async(body,key)=>showThread(await api(base+'/'+selected+'/reply',{body,key})));
        refresh().catch(err=>status(err.message));
        setInterval(()=>{if(!document.hidden)refresh().catch(err=>status(err.message));},10000);
    }
})();
