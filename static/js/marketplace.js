'use strict';
(() => {
 const state=JSON.parse(document.getElementById('marketplaceBootstrap').textContent);
 const list=document.getElementById('marketplaceBranches'),notice=document.getElementById('marketplaceNotice');
 const accountPanel=document.getElementById('marketplaceAccount'),passwordForm=document.getElementById('marketplacePasswordForm');
 const labels={active:'Подключено',pending:'Ожидает подключения',installing:'Подключение проверяется',unknown:'Результат не подтверждён',rejected:'YCLIENTS отклонил подключение',uninstalled:'Отключено',frozen:'Приостановлено'};
 const pausedHint='Подключение приостановлено. Если вы уже подключили филиал заново в YCLIENTS, нажмите «Проверить состояние». Иначе снова начните подключение из карточки GateBot в YCLIENTS.';
 const views=new Map();
 const fields=passwordForm.elements,sendCode=document.getElementById('marketplaceSendCode'),save=document.getElementById('marketplacePasswordSave'),codeStatus=document.getElementById('marketplaceCodeStatus');
 const verifyOriginal=document.getElementById('marketplaceVerifyOriginal'),codeLabel=document.getElementById('marketplaceCodeLabel');
 let busy=false,account=state.account||null,selectedId=null,formId=null,challenge=null,retryAt=0;
 const phoneKey=value=>window.GateBotPhone?.normalize(value)||value.replace(/[^0-9+]/g,'');
 const element=(tag,text,css)=>{const n=document.createElement(tag);n.textContent=text||'';if(css)n.className=css;return n;};
 const expired=()=>state.expired||Number(state.expires_at)<=Date.now()/1000;
 const onboarding=branch=>state.auto_onboarding&&!(state.authenticated&&branch.accessible&&!branch.pending_verification);
 function tell(text){notice.hidden=false;notice.textContent=text;}
 async function post(url,body){
  const response=await fetch(url+(window.marketplaceFrame?.query || ''),{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},body:JSON.stringify(body)});
  let data;
  try{data=await response.json();}catch(_){throw new Error('Не удалось получить ответ сервера. Обновите страницу, чтобы проверить состояние подключения.');}
  if(!response.ok){
   if(data.retry_after)retryAt=Date.now()+Number(data.retry_after)*1000;
   throw new Error(data.message||'Не удалось выполнить подключение. Попробуйте проверить его состояние.');
  }
  return data;
 }
 function showAccount(branch){
  if(branch.state!=='active')return;
  const canEnter=state.authenticated&&branch.accessible;
  const setup=branch.registration_required&&!canEnter;
  if(!canEnter&&!account&&!setup)return;
  accountPanel.hidden=false;
  document.getElementById('marketplaceAccountTitle').textContent=setup?'Регистрация':canEnter?'Всё готово':'Войдите в свой аккаунт';
  document.getElementById('marketplaceAccountText').textContent=setup?'Проверьте данные филиала и укажите контакты для входа в GateBot.':canEnter?'YCLIENTS подключён. Настройки филиала доступны в кабинете.':'Для доступа к кабинету войдите с вашим паролем. Если вы его не знаете, воспользуйтесь восстановлением на странице входа.';
  if(setup&&formId!==branch.id){
   formId=selectedId=branch.id;
   for(const key of ['name','email','phone','salon_name'])fields.namedItem(key).value=state.profile?.[key]||'';
   if(branch.otp){
    fields.phone.value=branch.otp.phone;
    challenge=branch.otp.ready?{id:branch.otp.challenge_id,phone:phoneKey(fields.phone.value),step:branch.otp.step}:null;
    retryAt=Date.now()+branch.otp.retry_after*1000;
    if(challenge)codeStatus.textContent=challenge.step==='original'?'Введите код с номера из YCLIENTS '+branch.otp.phone_hint+'.':'Введите ранее отправленный код.';
   }
  }
  passwordForm.hidden=!setup;
  document.getElementById('marketplaceAccountAction').hidden=Boolean(setup);
  const link=document.getElementById('marketplaceAccountLink');
  link.href=canEnter?'/admin/integrations':(window.marketplaceFrame?.loginUrl || '/admin/login?next=marketplace');
  link.textContent=canEnter?'Открыть кабинет':'Войти в GateBot';
 }
 function updateActions(){
  if(!passwordForm.isConnected)return;
  const originalPhone=phoneKey(state.profile?.phone||'');
  document.getElementById('marketplacePhoneChange').hidden=!/^\+[1-9]\d{6,14}$/.test(originalPhone)||!fields.phone.value||phoneKey(fields.phone.value)===originalPhone;
  const retry=Math.max(0,Math.ceil((retryAt-Date.now())/1000));
  sendCode.disabled=busy||expired()||retry>0;
  sendCode.textContent=retry?'Повторить через '+retry+' с':challenge?'Отправить код ещё раз':'Получить код по SMS';
  const readyForNumber=challenge&&phoneKey(fields.phone.value)===challenge.phone;
  const original=readyForNumber&&challenge.step==='original';
  verifyOriginal.hidden=!original;
  verifyOriginal.disabled=busy||expired()||!original;
  codeLabel.textContent=original?'Код для номера из YCLIENTS':'Код из SMS';
  save.disabled=busy||expired()||!readyForNumber||original;
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
   branch.registration_required=Boolean(data.registration_required);
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
  }else if(branch.pending_verification){panel.append(element('p','Подключение филиала не завершено. Обратитесь в поддержку GateBot.'));}
  else if(state.authenticated){panel.append(element('p','У вашего аккаунта нет прав на этот филиал. Получите приглашение от его администратора или зарегистрируйте новый филиал.'));}
  list.append(panel);
  if(branch.state==='active'){
   if(state.auto_onboarding&&account?.branch_id===branch.id)selectedId=branch.id;
   if(!account?.branch_id||account.branch_id===branch.id)showAccount(branch);
  }
 }
 fields.phone.addEventListener('input',updateActions);
 async function sendRegistrationCode(verify=false){
  if((verify?verifyOriginal.disabled:sendCode.disabled)||!fields.phone.reportValidity())return;
  if(verify&&!fields.code.reportValidity())return;
  const phone=fields.phone.value;
  const body={branch_id:formId,phone};
  if(verify){body.original_challenge=challenge.id;body.original_code=fields.code.value;}
  busy=true;updateActions();
  try{
   const data=await post('/yclients/marketplace/code',body);
   retryAt=Date.now()+data.retry_after*1000;
   challenge=data.ready?{id:data.challenge_id,phone:phoneKey(phone),step:data.step}:null;
   fields.code.value='';
   codeStatus.textContent=data.ready?(data.step==='original'?'Для замены телефона введите код с номера из YCLIENTS ':'Код отправлен на номер ')+data.phone_hint+'.':'Отправить SMS не удалось. Повторите попытку.';
   if(data.ready)fields.code.focus();
  }catch(error){codeStatus.textContent=error.message;}finally{busy=false;updateActions();}
 }
 sendCode.addEventListener('click',()=>sendRegistrationCode());
 verifyOriginal.addEventListener('click',()=>sendRegistrationCode(true));
 passwordForm.addEventListener('submit',async event=>{
  event.preventDefault();
  if(save.disabled||passwordForm.hidden||!passwordForm.reportValidity())return;
  const password=document.getElementById('marketplacePassword'),confirm=document.getElementById('marketplacePasswordConfirm');
  if(password.value!==confirm.value){tell('Пароли не совпадают. Повторите пароль ещё раз.');confirm.focus();return;}
  const button=document.getElementById('marketplacePasswordSave');
  busy=true;button.disabled=true;updateActions();
  try{
   const profile=Object.fromEntries(['name','salon_name','phone','email'].map(key=>[key,fields.namedItem(key).value]));
   const data=await post('/yclients/marketplace/register',{branch_id:formId,profile,challenge:challenge.id,code:fields.code.value,password:password.value,confirm:confirm.value});
   password.value='';confirm.value='';
   // The completion target is fixed to the GateBot cabinet, never an external URL.
   if(data.redirect_url!=='/admin/integrations')throw new Error('Пароль сохранён. Откройте кабинет через страницу входа.');
   if(window.marketplaceFrame){clearInterval(timer);window.marketplaceFrame.completed();return;}
   window.location.assign(data.redirect_url);
  }catch(error){tell(error.message);}finally{busy=false;button.disabled=false;updateActions();}
 });
 function tick(){
  const left=state.expired?0:Math.max(0,Math.ceil(Number(state.expires_at)-Date.now()/1000));
  const completed=state.branches.some(branch=>branch.state==='active'&&!branch.registration_required);
  document.getElementById('marketplaceDeadline').textContent=completed?'':left?'Разрешение YCLIENTS действует ещё '+Math.floor(left/60)+' мин '+left%60+' с.':'Время вышло. Снова нажмите «Подключить» в YCLIENTS.';
  updateActions();
 }
 if(!state.configured)tell('Подключение в маркетплейсе ещё не настроено владельцем GateBot.');
 else if(!state.enabled)tell('Новые подключения пока выключены владельцем GateBot.');
 else if(state.branches.some(branch=>['uninstalled','frozen'].includes(branch.state)))tell(pausedHint);
 tick();const timer=setInterval(tick,1000);
 if(state.auto_onboarding&&state.configured&&state.enabled&&!expired()&&state.branches.length===1){
  const branch=state.branches[0];
  if(branch.state!=='active'||(!account&&!branch.registration_required))connect(branch,['unknown','installing','uninstalled','frozen'].includes(branch.state));
 }
})();
