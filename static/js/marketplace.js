'use strict';
(() => {
 const state=JSON.parse(document.getElementById('marketplaceBootstrap').textContent);
 const list=document.getElementById('marketplaceBranches'),notice=document.getElementById('marketplaceNotice');
 const labels={active:'Подключено',pending:'Ожидает подключения',installing:'Результат подключения ещё не подтверждён',unknown:'Результат не подтверждён',rejected:'YCLIENTS отклонил подключение',uninstalled:'Отключено',frozen:'Приостановлено'};
 const pausedHint='Подключение приостановлено. Подтверждение отключения могло прийти с задержкой. Если вы уже подключили филиал заново, нажмите «Проверить состояние».';
 const element=(tag,text,css)=>{const n=document.createElement(tag);n.textContent=text||'';if(css)n.className=css;return n;};
 function tell(text){notice.hidden=false;notice.textContent=text;}
 async function connect(branch,reconcile,button,status){
  button.disabled=true;
  try{
   const response=await fetch('/yclients/marketplace/connect',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},body:JSON.stringify({branch_id:branch.id,reconcile})});
   const data=await response.json();if(!response.ok)throw new Error(data.message||'Не удалось выполнить подключение');
   branch.state=data.state;status.textContent=labels[data.state]||'Состояние уточняется';
   tell(data.state==='active'?'Филиал подключён к YCLIENTS.':data.state==='unknown'||data.state==='installing'?'Ответ не подтверждён. Проверьте состояние подключения; повторная активация автоматически не отправляется.':['uninstalled','frozen'].includes(data.state)?pausedHint:'Проверьте права приложения и снова начните подключение из карточки GateBot в YCLIENTS.');
  }catch(error){tell(error.message);}finally{button.disabled=false;tick();}
 }
 for(const branch of state.branches){
  const panel=element('section','','panel');panel.append(element('h2',branch.name||'Филиал '+branch.id));
  const status=element('p',labels[branch.state]||'Ожидает подключения');panel.append(status);
  if(state.authenticated&&branch.accessible&&!branch.pending_verification){
   const button=element('button','Подключить','btn-primary');button.type='button';button.dataset.install='1';button.onclick=()=>connect(branch,false,button,status);panel.append(button);
   const check=element('button','Проверить состояние','btn-action');check.type='button';check.disabled=!state.configured;check.onclick=()=>connect(branch,true,check,status);panel.append(document.createTextNode(' '),check);
  } else if(branch.pending_verification){panel.append(element('p','Филиал ожидает проверки суперадминистратором. После подтверждения снова начните подключение в YCLIENTS.'));}
  else if(state.authenticated){panel.append(element('p','У вашего аккаунта нет прав на этот филиал. Получите приглашение от его администратора или зарегистрируйте новый филиал.'));}
  list.append(panel);
 }
 if(!state.configured)tell('Подключение в маркетплейсе ещё не настроено владельцем GateBot.');
 else if(state.branches.some(branch=>branch.accessible&&['uninstalled','frozen'].includes(branch.state)))tell(pausedHint);
 else if(!state.enabled)tell('Новые подключения пока выключены владельцем GateBot. Состояние существующего подключения можно проверить.');
 function tick(){const left=Math.max(0,Math.ceil(state.expires_at-Date.now()/1000));document.getElementById('marketplaceDeadline').textContent=left?'Время на подключение: '+Math.floor(left/60)+' мин '+left%60+' с':'Время вышло. Снова нажмите «Подключить» в YCLIENTS.';document.querySelectorAll('[data-install]').forEach(button=>{if(!left||!state.enabled||!state.configured)button.disabled=true;});}
 tick();setInterval(tick,1000);
})();
