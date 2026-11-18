'use strict';
(() => {
  const $=id=>document.getElementById(id);if(!$('supportApp'))return;
  const dialog=$('supportDialog'),launcher=$('supportLauncher'),form=$('supportForm'),messages=$('supportMessages');
  let selected=null,generation=0,initialized=false,timer=null,polling=false,sending=false,nextBefore=null,pending=null,lastId=0,firstId=0,isSuper=false;
  const element=(tag,text='',className='')=>{const n=document.createElement(tag);n.textContent=text;n.className=className;return n;};
  const key=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,'0')).join('');
  const tell=text=>{$('supportNotice').textContent=text;$('supportNotice').hidden=!text;};
  async function api(path,data){
    const response=await fetch(path,{method:data===undefined?'GET':'POST',credentials:'same-origin',headers:data===undefined?{}:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name=csrf-token]').content},body:data===undefined?undefined:JSON.stringify(data)});
    if(!response.headers.get('content-type')?.includes('application/json'))throw Error('Сессия истекла. Обновите страницу и войдите в кабинет.');
    const value=await response.json();if(!response.ok)throw Error(value.message||'Не удалось выполнить действие. Повторите позже.');return value;
  }
  function renderThreads(items,append=false){
    if(!append)$('supportThreads').replaceChildren();
    for(const item of items){
      let button=$('supportThreads').querySelector('[data-thread="'+item.id+'"]');if(!button){button=element('button','','support-thread');button.type='button';button.dataset.thread=item.id;$('supportThreads').append(button);}
      button.replaceChildren(element('strong','№'+item.id+' · '+item.title),element('span',item.branch_name+' · '+item.owner_name));
      if(item.unread)button.append(element('small','Новых сообщений: '+item.unread,'support-unread'));
      else button.append(element('small',item.state==='closed'?'Закрыто':item.last_message?.sender_role==='superadmin'?'Есть ответ поддержки':'Открыто'));
      button.setAttribute('aria-current',String(item.id===selected));button.onclick=()=>choose(item.id);
    }
    if(!$('supportThreads').children.length)$('supportThreads').append(element('p','Обращений пока нет','page-description'));
  }
  async function index(append=false){
    const result=await api('/admin/api/support'+(append&&nextBefore?'?before='+nextBefore:''));
    isSuper=result.is_super;$('supportBranchLabel').hidden=!!selected||!isSuper;
    renderThreads(result.items,append);nextBefore=result.next_before;$('supportMore').hidden=!nextBefore;
    const previous=$('supportBranch').value;$('supportBranch').replaceChildren();
    for(const branch of result.branches){const option=element('option',branch.name);option.value=branch.id;$('supportBranch').append(option);}
    if(result.branches.some(branch=>branch.id===previous))$('supportBranch').value=previous;
    form.querySelector('[type=submit]').disabled=sending||!result.branches.length;
  }
  function renderMessages(rows,prepend=false){
    const fragment=document.createDocumentFragment();
    for(const item of rows){if(messages.querySelector('[data-message="'+item.id+'"]'))continue;
      const bubble=element('article','','support-message'+(item.mine?' mine':''));bubble.dataset.message=item.id;
      bubble.append(element('p',item.text),element('small',(item.sender_role==='superadmin'?'Поддержка':'Администратор')+' · '+new Date(item.created*1000).toLocaleString()));fragment.append(bubble);
      lastId=Math.max(lastId,item.id);firstId=firstId?Math.min(firstId,item.id):item.id;
    }
    if(prepend)messages.prepend(fragment);else messages.append(fragment);
  }
  async function loadThread(after=0,before=null){
    if(!selected)return;const id=selected,version=generation;
    const result=await api('/admin/api/support/threads/'+id+(before?'?before='+before:after?'?after='+after:''));
    if(id!==selected||version!==generation)return;
    $('supportTitle').textContent='№'+id+' · '+result.thread.title;
    $('supportContext').textContent=result.thread.branch_name+' · '+result.thread.owner_name+(result.thread.state==='closed'?' · Обращение закрыто':'');
    $('supportCloseThread').hidden=false;$('supportCloseThread').textContent=result.thread.state==='closed'?'Открыть снова':'Закрыть обращение';$('supportCloseThread').dataset.state=result.thread.state;
    form.hidden=!result.thread.can_reply;
    if(!after&&!before){messages.replaceChildren();lastId=0;firstId=0;}
    const nearBottom=messages.scrollHeight-messages.scrollTop-messages.clientHeight<90;
    renderMessages(result.messages,!!before);
    if(!after)$('supportOlder').hidden=!result.has_older;
    if(!before&&(nearBottom||!after))messages.scrollTop=messages.scrollHeight;
    if(result.messages.length)await api('/admin/api/support/threads/'+id+'/read',{message_id:result.messages[result.messages.length-1].id});
  }
  async function choose(id){
    generation++;selected=Number(id);pending=null;tell('');$('supportBranchLabel').hidden=true;form.hidden=false;form.reset();
    for(const button of $('supportThreads').querySelectorAll('[data-thread]'))button.setAttribute('aria-current',String(Number(button.dataset.thread)===selected));
    try{await loadThread();}catch(error){tell(error.message);}
  }
  function fresh(){generation++;selected=null;pending=null;lastId=0;firstId=0;messages.replaceChildren();form.reset();form.hidden=false;$('supportBranchLabel').hidden=!isSuper;$('supportCloseThread').hidden=true;$('supportOlder').hidden=true;$('supportTitle').textContent='Новое обращение';$('supportContext').textContent='Опишите проблему; ответ появится здесь.';tell('');for(const button of $('supportThreads').querySelectorAll('[data-thread]'))button.setAttribute('aria-current','false');$('supportText').focus();}
  async function poll(){if(polling||document.hidden||(dialog&&!dialog.open))return;polling=true;try{if(selected)await loadThread(lastId);await index();}catch(error){tell(error.message);}finally{polling=false;}}
  async function start(){
    try{await index();initialized=true;if(!selected)fresh();clearInterval(timer);timer=setInterval(poll,5000);}catch(error){tell(error.message);}
  }
  form.onsubmit=async event=>{
    event.preventDefault();if(sending)return;const text=$('supportText').value.trim();if(!text)return;
    const version=generation,id=selected,branch=$('supportBranch').value;
    if(!pending||pending.text!==text||pending.thread_id!==id||pending.branch_id!==branch)pending={text,thread_id:id,branch_id:branch,request_key:key()};
    const payload={...pending};if(!id)delete payload.thread_id;sending=true;form.querySelector('[type=submit]').disabled=true;tell('');
    try{const result=await api('/admin/api/support/messages',payload);if(version!==generation)return;pending=null;$('supportText').value='';selected=result.thread_id;$('supportBranchLabel').hidden=true;await loadThread();await index();}
    catch(error){if(version===generation)tell(error.message+' Повторная отправка этого текста не создаст дубликат.');}
    finally{sending=false;form.querySelector('[type=submit]').disabled=!$('supportBranch').options.length;}
  };
  $('supportNew').onclick=fresh;
  $('supportMore').onclick=()=>index(true).catch(error=>tell(error.message));
  $('supportOlder').onclick=()=>loadThread(0,firstId).catch(error=>tell(error.message));
  $('supportCloseThread').onclick=async()=>{if(!selected)return;const id=selected;try{await api('/admin/api/support/threads/'+id+'/state',{state:$('supportCloseThread').dataset.state==='closed'?'open':'closed'});if(id===selected)await loadThread(lastId);await index();}catch(error){tell(error.message);}};
  if(dialog){launcher.onclick=()=>{dialog.showModal();if(!initialized)start();else{poll();clearInterval(timer);timer=setInterval(poll,5000);}};$('supportDismiss').onclick=()=>dialog.close();dialog.addEventListener('close',()=>{clearInterval(timer);launcher.focus();});}
  else start();
  window.addEventListener('pagehide',()=>clearInterval(timer));
})();
