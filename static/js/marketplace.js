'use strict';
(() => {
 const state=JSON.parse(document.getElementById('marketplaceBootstrap').textContent);
 const list=document.getElementById('marketplaceBranches'),notice=document.getElementById('marketplaceNotice');
 const accountPanel=document.getElementById('marketplaceAccount'),passwordForm=document.getElementById('marketplacePasswordForm');
 const labels={active:'Подключено',pending:'Ожидает подключения',installing:'Подключение проверяется',unknown:'Результат не подтверждён',rejected:'YCLIENTS отклонил подключение',uninstalled:'Отключено',frozen:'Приостановлено'};
 const pausedHint='Подключение приостановлено. Если вы уже подключили филиал заново в YCLIENTS, нажмите «Проверить состояние». Иначе снова начните подключение из карточки GateBot в YCLIENTS.';
 const views=new Map();
 let busy=false,account=state.account||null,selectedId=null;
 const element=(tag,text,css)=>{const n=document.createElement(tag);n.textContent=text||'';if(css)n.className=css;return n;};
 const expired=()=>state.expired||Number(state.expires_at)<=Date.now()/1000;
 const onboarding=branch=>state.auto_onboarding&&!(state.authenticated&&branch.accessible&&!branch.pending_verification);
 function tell(text){notice.hidden=false;notice.textContent=text;}
 async function post(url,body){
  const response=await fetch(url,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},body:JSON.stringify(body)});
  let data;
  try{data=await response.json();}catch(_){throw new Error('Не удалось получить ответ сервера. Обновите страницу, чтобы проверить состояние подключения.');}
  if(!response.ok)throw new Error(data.message||'Не удалось выполнить подключение. Попробуйте проверить его состояние.');
  return data;
 }
 function showAccount(branch){
  if(branch.state!=='active')return;
  const canEnter=state.authenticated&&branch.accessible;
  if(!canEnter&&!account)return;
  accountPanel.hidden=false;
  const setup=!canEnter&&account&&account.can_set_password;
  document.getElementById('marketplaceAccountTitle').textContent=setup?'Придумайте пароль':canEnter?'Всё готово':'Войдите в свой аккаунт';
  document.getElementById('marketplaceAccountText').textContent=setup?'Филиал подключён. Ваш логин: '+account.username+'. Сохраните пароль, чтобы пользоваться кабинетом.':canEnter?'YCLIENTS подключён. Настройки филиала доступны в кабинете.':'Филиал подключён. Для доступа к существующему аккаунту войдите с вашим паролем. Если вы его не знаете, воспользуйтесь восстановлением на странице входа.';
  passwordForm.hidden=!setup;
  document.getElementById('marketplaceAccountAction').hidden=Boolean(setup);
  const link=document.getElementById('marketplaceAccountLink');
  link.href=canEnter?'/admin/integrations':'/admin/login?next=marketplace';
  link.textContent=canEnter?'Открыть кабинет':'Войти в GateBot';
 }
 function updateActions(){
  for(const view of views.values()){
   const {branch,install,check}=view;
   view.status.textContent=labels[branch.state]||'Состояние уточняется';
   view.status.className='badge '+(branch.state==='active'?'success':['rejected','uninstalled'].includes(branch.state)?'danger':'warning');
   if(install){
    install.hidden=state.auto_onboarding&&['active','installing','unknown','uninstalled','frozen','rejected'].includes(branch.state);
    install.disabled=busy||expired()||!state.enabled||!state.configured||(state.auto_onboarding&&selectedId!==null&&selectedId!==branch.id);
   }
   if(check){
    check.hidden=state.auto_onboarding&&!['unknown','installing','uninstalled','frozen','rejected'].includes(branch.state);
    check.disabled=busy||!state.configured||(state.auto_onboarding&&(!state.enabled||expired()||(selectedId!==null&&selectedId!==branch.id)));
   }
  }
 }
 async function connect(branch,reconcile=false){
  if(busy)return;
  if(state.auto_onboarding&&(expired()||!state.enabled||!state.configured))return;
  if(!reconcile&&(expired()||!state.enabled||!state.configured))return;
  busy=true;
  if(state.auto_onboarding)selectedId=branch.id;
  updateActions();
  tell(reconcile?'Проверяем подключение в YCLIENTS…':'Подключаем филиал к YCLIENTS…');
  try{
   const data=await post(onboarding(branch)?'/yclients/marketplace/onboard':'/yclients/marketplace/connect',{branch_id:branch.id,reconcile});
   branch.state=data.state;
   if(data.account)account=data.account;
   tell(data.state==='active'?'Филиал подключён к YCLIENTS.':['unknown','installing'].includes(data.state)?'Ответ пока не подтверждён. Нажмите «Проверить состояние». Повторную активацию автоматически не отправляем.':['uninstalled','frozen'].includes(data.state)?pausedHint:'Подключение не завершено. Проверьте права приложения и снова начните подключение из карточки GateBot в YCLIENTS.');
   showAccount(branch);
  }catch(error){
   // A transport failure does not prove that installation failed on YCLIENTS.
   branch.state='unknown';
   tell(error.message);
  }finally{busy=false;updateActions();tick();}
 }
 for(const branch of state.branches){
  const panel=element('section','','panel');panel.append(element('h2',branch.name||'Филиал '+branch.id));
  const status=element('p',labels[branch.state]||'Ожидает подключения');panel.append(status);
  const view={branch,status,install:null,check:null};views.set(branch.id,view);
  if(state.auto_onboarding||(state.authenticated&&branch.accessible&&!branch.pending_verification)){
   const actions=element('div','','filter-section');
   const button=element('button',state.auto_onboarding&&state.branches.length>1?'Подключить этот филиал':'Подключить','btn-primary');button.type='button';button.addEventListener('click',()=>connect(branch));view.install=button;actions.append(button);
   const check=element('button','Проверить состояние','btn-action');check.type='button';check.addEventListener('click',()=>connect(branch,true));view.check=check;actions.append(check);
   panel.append(actions);
  }else if(branch.pending_verification){panel.append(element('p','Филиал ожидает проверки суперадминистратором. После подтверждения снова начните подключение в YCLIENTS.'));}
  else if(state.authenticated){panel.append(element('p','У вашего аккаунта нет прав на этот филиал. Получите приглашение от его администратора или зарегистрируйте новый филиал.'));}
  list.append(panel);
  if(branch.state==='active'){
   if(state.auto_onboarding&&account)selectedId=branch.id;
   showAccount(branch);
  }
 }
 passwordForm.addEventListener('submit',async event=>{
  event.preventDefault();
  if(busy||passwordForm.hidden||!passwordForm.reportValidity())return;
  const password=document.getElementById('marketplacePassword'),confirm=document.getElementById('marketplacePasswordConfirm');
  if(password.value!==confirm.value){tell('Пароли не совпадают. Повторите пароль ещё раз.');confirm.focus();return;}
  const button=document.getElementById('marketplacePasswordSave');
  busy=true;button.disabled=true;updateActions();
  try{
   const data=await post('/yclients/marketplace/password',{password:password.value,confirm:confirm.value});
   password.value='';confirm.value='';
   // The completion target is fixed to the GateBot cabinet, never an external URL.
   if(data.redirect_url!=='/admin/integrations')throw new Error('Пароль сохранён. Откройте кабинет через страницу входа.');
   window.location.assign(data.redirect_url);
  }catch(error){tell(error.message);}finally{busy=false;button.disabled=false;updateActions();}
 });
 function tick(){
  const left=state.expired?0:Math.max(0,Math.ceil(Number(state.expires_at)-Date.now()/1000));
  const completed=state.branches.some(branch=>branch.state==='active');
  document.getElementById('marketplaceDeadline').textContent=completed?'':left?'Разрешение YCLIENTS действует ещё '+Math.floor(left/60)+' мин '+left%60+' с.':'Время вышло. Снова нажмите «Подключить» в YCLIENTS.';
  updateActions();
 }
 if(!state.configured)tell('Подключение в маркетплейсе ещё не настроено владельцем GateBot.');
 else if(!state.enabled)tell('Новые подключения пока выключены владельцем GateBot.');
 else if(state.branches.some(branch=>['uninstalled','frozen'].includes(branch.state)))tell(pausedHint);
 tick();setInterval(tick,1000);
 if(state.auto_onboarding&&state.configured&&state.enabled&&!expired()&&state.branches.length===1){
  const branch=state.branches[0];
  if(branch.state!=='active'||!account)connect(branch,['unknown','installing'].includes(branch.state));
 }
})();
