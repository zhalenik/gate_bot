'use strict';
(() => {
 const boot=JSON.parse(document.getElementById('identityBootstrap').textContent),data=boot.data,root=document.getElementById('identityContent');
 const notice=document.getElementById('identityNotice'),description=document.getElementById('identityDescription');
 const labels={email:'Email',telegram:'Telegram',max:'MAX',chatpush:'ChatPush',sms:'SMS'};
 const events={gate_requested:'Запрос открытия ворот',gate_denied:'Отказ в доступе',gate_error:'Ошибка открытия',admin_contact_requested:'Запрос контакта администратора',invitation_accepted:'Приглашение принято',access_changed:'Изменение пропуска',crm_error:'Ошибка CRM',crm_recovered:'CRM восстановлена',modem_error:'Ошибка модема',modem_recovered:'Модем восстановлен',delivery_failed:'Ошибка доставки',subscription_expiring:'Подписка заканчивается',subscription_expired:'Подписка закончилась',quota_warning:'Квота заканчивается',quota_exhausted:'Квота исчерпана',branch_registered:'Регистрация нового филиала',marketplace_disconnected:'Отключение в маркетплейсе YCLIENTS',sms_received:'Входящие SMS',call_received:'Входящие звонки'};
 Object.assign(events,{client_message:'Сообщение клиента',pass_requested:'Запрос пропуска',pass_request_auto_granted:'Пропуск под запрос выдан автоматически'});
 function tell(text,error=false){notice.hidden=false;notice.textContent=text;notice.className='panel '+(error?'alert-error':'alert-success');}
 function field(form,label,name,type='text',required=true,value='') {const wrap=element('div','form-group'),caption=element('label','',label),input=element('input');input.name=name;input.type=type;input.required=required;input.value=value;if(type==='password')input.autocomplete='new-password';caption.append(input);wrap.append(caption);form.append(wrap);return input;}
 function select(form,label,name,items){const wrap=element('div','form-group'),caption=element('label','',label),node=element('select');node.name=name;for(const [key,text] of items){const option=element('option','',text);option.value=key;node.append(option);}caption.append(node);wrap.append(caption);form.append(wrap);return node;}
 function check(form,label,name,value){const wrap=element('label','checkbox-label'),node=element('input');node.type='checkbox';node.name=name;node.checked=Boolean(value);wrap.append(node,document.createTextNode(' '+label));form.append(wrap);return node;}
 function form(){const value=element('form','admin-form');root.append(value);return value;}
 function submit(form,label,handler){const control=element('button','btn-primary',label);control.type='submit';form.append(control);form.addEventListener('submit',async event=>{event.preventDefault();control.disabled=true;try{await handler(Object.fromEntries(new FormData(form)));}catch(error){tell(error.message,true);}finally{control.disabled=false;}});return control;}
 function action(parent,label,handler){const control=button(label,async()=>{control.disabled=true;try{await handler();}catch(error){tell(error.message,true);}finally{control.disabled=false;}},'btn-action');parent.append(control);return control;}
 function branch(form){if(!boot.is_super){const input=element('input');input.type='hidden';input.name='branch_id';input.value=boot.branches[0]?.id||'';form.append(input);return input;}return select(form,'Филиал','branch_id',boot.branches.map(b=>[b.id,b.name]));}
 function delivery(result){const d=result.delivery || result;return ['sent','success','queued','accepted','network_accepted'].includes(d.status)?'Сообщение принято к отправке':'Отправка не подтверждена: '+(d.code || d.status || 'неизвестный результат');}
 function botActions(parent,admin=false){for(const platform of ['telegram','max'])action(parent,'Подключить '+labels[platform],async()=>{const result=await post(admin?'/admin/api/identity/bot-link':'/preferences/'+data.token+'/bot-link',{platform});location.assign(result.link);});}
 if(boot.kind==='invitations'){
  description.textContent='Приглашение действует один час. Права появятся после подтверждения телефона.';
  const f=form(),invitationBranch=branch(f);field(f,'Телефон','phone','tel',false);field(f,'Email','email','email',false);const inviteChannel=select(f,'Отправить через','channel',Object.entries(labels).filter(([k])=>!['telegram','max'].includes(k)));
  const copied=element('div','invitation-copy');
  if(boot.is_super)invitationBranch.setAttribute('aria-label','Филиал');
  root.append(copied);
  const admins=document.getElementById('adminMemberList'), invites=document.getElementById('invitationList');
  const inviteStates={pending:'Ожидает регистрации',accepted:'Принято',expired:'Срок истёк',revoked:'Отозвано'};
  const dateFormat=new Intl.DateTimeFormat('ru-RU',{dateStyle:'short',timeStyle:'short'});
  let revision=0, selection=0;

  function tableStatus(body,message){
   const status=document.getElementById(body.id+'Status');
   status.textContent=message;
   status.hidden=!message;
  }
  function appendRow(body,values){
   const row=body.insertRow(),headings=body.closest('table').tHead.rows[0].cells;
   for(const [i,value] of values.entries()){
    const cell=row.insertCell();
    cell.dataset.label=headings[i].textContent;
    cell.textContent=value || '—';
   }
   const actions=row.insertCell();
   actions.dataset.label='Действие';
   return actions;
  }
  async function changeRole(url,payload){
   const original=selection;
   try{await post(url,payload);}
   catch(error){if(original===selection)tell(error.message,true);return false;}
   if(original!==selection)return false;
   await refresh();
   return original===selection;
  }
  function displayAdmins(rows,selected){
   admins.replaceChildren();
   tableStatus(admins,rows.length?'':'В филиале пока нет администраторов.');
   const canRevoke=rows.filter(row=>row.status==='active').length>1;
   for(const row of rows){
    const active=row.status==='active';
    const actions=appendRow(admins,[row.name,row.phone,active?'Активен':'Права отозваны']);
    if(!active)continue;
    if(!canRevoke){actions.textContent='Единственный администратор';continue;}
    action(actions,'Отозвать права',async()=>{
     if(!confirm('Отозвать права администратора этого филиала?'))return;
     if(await changeRole('/admin/api/identity/admins/revoke',{person_id:row.person_id,branch_id:selected}))
      tell('Права отозваны. Обычный пропуск сохранён.');
    });
   }
  }
  function displayInvitations(rows){
   invites.replaceChildren();
   tableStatus(invites,rows.length?'':'Приглашений пока нет.');
   for(const row of rows){
    const actions=appendRow(invites,[row.phone,row.email,dateFormat.format(row.expires*1000),inviteStates[row.state] || row.state]);
    if(row.state==='pending')action(actions,'Отозвать',async()=>{
     if(await changeRole('/admin/api/identity/invitations/'+encodeURIComponent(row.id)+'/revoke',{}) && copied.dataset.id===row.id)
      copied.replaceChildren();
    });
   }
  }
  async function refresh(){
   const serial=++revision,selected=invitationBranch.value;
   if(!selected)return;
   async function load(body,path,paint){
    try{
     const result=await api('/admin/api/identity/'+path+'?branch_id='+encodeURIComponent(selected));
     if(serial===revision)paint(result[path],selected);
    }catch(error){
     if(serial===revision)tableStatus(body,error.message);
    }
   }
   await Promise.all([load(admins,'admins',displayAdmins),load(invites,'invitations',displayInvitations)]);
  }
  function selectBranch(){
   selection++;
   notice.hidden=true;
   copied.replaceChildren();
   for(const body of [admins,invites]){
    body.replaceChildren();
    tableStatus(body,invitationBranch.value?'Загрузка…':'Сначала создайте филиал.');
   }
   const standalone=boot.branches.find(b=>String(b.id)===invitationBranch.value)?.yclients_enabled===false;
   const chatpush=inviteChannel.querySelector('option[value=chatpush]');
   chatpush.hidden=chatpush.disabled=standalone;
   if(standalone&&inviteChannel.value==='chatpush')inviteChannel.value='sms';
   create.disabled=!invitationBranch.value;
   refresh();
  }
  const create=submit(f,'Создать приглашение',async values=>{
   const original=selection;
   let result;
   try{result=await post('/admin/invitations/new',values);}
   catch(error){if(original===selection)tell(error.message,true);return;}
   if(original!==selection)return;
   tell(delivery(result));
   copied.dataset.id=result.invitation_id;
   copied.replaceChildren(element('p','',result.text));
   action(copied,'Скопировать приглашение',()=>copyToClipboard(result.text));
   await refresh();
  });
  invitationBranch.addEventListener('change',selectBranch);
  selectBranch();
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
  const refreshTimer=setInterval(()=>{if(!document.hidden)refresh();},60000);
  window.addEventListener('pagehide',()=>clearInterval(refreshTimer),{once:true});
 } else if(boot.kind==='accept'||boot.kind==='register'){
  const registration=boot.kind==='register';description.textContent=registration?'Создайте аккаунт администратора филиала.':'Филиал: '+data.branch_name;
  const initial=data.initial||{},f=form();f.autocomplete='off';let registrationMode=null,companyId=null;
  function describeMode(){if(!registration)return;const crm=registrationMode.value==='true';companyId.closest('.form-group').hidden=!crm;companyId.required=crm;if(!crm)companyId.value='';}
  if(registration){
   registrationMode=select(f,'Как будет работать филиал','yclients_enabled',[['true','YCLIENTS — учётная запись пользователя'],['false','Без CRM — самостоятельный филиал']]);companyId=field(f,'ID филиала YCLIENTS','branch_id','number',true);
   field(f,'Название филиала','branch_name','text',true,initial.branch_name||'');describeMode();
  }
  field(f,'Ваше имя','name','text',true,initial.name||'');if(!data.phone_fixed)field(f,'Телефон','phone','tel',true,initial.phone||'');
  const email=field(f,'Email — ваш логин','username','email',true,initial.email||'');email.maxLength=254;email.autocomplete='email';
  const password=field(f,'Пароль GateBot (от 12 символов)','password','password');password.minLength=12;password.autocomplete='new-password';
  if(!registration)field(f,'Текущий пароль, если аккаунт уже существует','existing_password','password',false);
  let registrationToken='',challenge='',sending=false,deadline=0,waiting=false,statusBusy=false,ready=false,finished=false,flowVersion=0;
  const code=field(f,'Код из SMS','code','text',false);code.inputMode='numeric';code.maxLength=6;code.autocomplete='one-time-code';code.pattern='[0-9]{6}';
  const codeStatus=element('p','page-description');codeStatus.id='otpDeliveryStatus';codeStatus.setAttribute('role','status');f.append(codeStatus);
  const resend=element('button','btn-action','Получить SMS-код');resend.type='button';resend.id='otpResend';f.append(resend);
  // Clear obsolete drafts from older versions. New flows live only in memory.
  try{sessionStorage.removeItem('gatebot.registration.pending');sessionStorage.removeItem('gatebot.otp.resend:'+location.pathname);}catch{}
  const verifiedFields=['branch_id','branch_name','name','phone','yclients_enabled','username'];
  function invalidate(){if(finished)return;flowVersion++;registrationToken='';challenge='';ready=false;waiting=false;sending=false;code.value='';code.required=false;codeStatus.textContent='';tell('Данные изменены. Запросите новый SMS-код.');paint();}
  for(const name of verifiedFields){const input=f.elements[name];if(!input)continue;input.addEventListener(input.tagName==='SELECT'?'change':'input',()=>{if(input===registrationMode)describeMode();if(registrationToken||challenge||sending)invalidate();});}
  function cooldown(seconds){deadline=performance.now()+Math.max(0,Number(seconds)||0)*1000;paint();}
  function paint(){const seconds=Math.max(0,Math.ceil((deadline-performance.now())/1000));resend.disabled=sending||seconds>0||finished;resend.textContent=sending?'Отправляем…':seconds?`Отправить повторно через ${seconds} с`:challenge?'Отправить код ещё раз':'Получить SMS-код';}
  function received(result){
   if(!result.challenge_id)return;challenge=result.challenge_id;ready=result.ready===true;code.required=true;cooldown(result.retry_after);
   const state=result.delivery?.delivery||result.delivery?.status||'unknown';waiting=['queued','sending','pending','processing','success'].includes(state);
   const hints={queued:'Код в очереди отправки.',success:'Код в очереди отправки.',sending:'Отправляем код.',processing:'Отправляем код.',network_accepted:'SMS передана оператору. Введите код после получения.',accepted:'SMS принята к отправке.',sent:'SMS отправлена.',unknown:'Результат отправки не подтверждён. Если код не придёт, запросите новый после таймера.',failed:'Не удалось отправить SMS. Повторите после таймера или обратитесь к администратору.',error:'Не удалось отправить SMS. Проверьте модем у администратора.',expired:'Время ожидания отправки истекло. Запросите новый код.',cancelled:'Отправка отменена. Запросите новый код.'};
   codeStatus.textContent=(result.phone_hint?'Телефон '+result.phone_hint+'. ':'')+(hints[state]||'Отправка не подтверждена.');
   if(result.delivery?.code==='outbound_disabled')codeStatus.textContent='Отправка SMS выключена в настройках сервиса. Обратитесь к администратору.';
   if(result.delivery?.code==='hardware_required')codeStatus.textContent='Модем работает в тестовом режиме. Для получения кода администратор должен включить реальную отправку.';
  }
  async function refreshCode(){if(statusBusy||sending||finished||!challenge||(registration&&!registrationToken))return;const version=flowVersion,token=registrationToken;statusBusy=true;try{const result=await post(registration?'/register-branch/code-status':'/invite/'+data.token+'/code-status',registration?{registration_token:token}:{});if(version===flowVersion&&token===registrationToken&&!sending&&!finished)received(result);}catch{}finally{statusBusy=false;}}
  resend.addEventListener('click',async()=>{
   if(sending||finished||performance.now()<deadline)return;
   for(const name of verifiedFields){const input=f.elements[name];if(input&&!input.disabled&&!input.reportValidity())return;}
   const version=++flowVersion;sending=true;paint();
   try{const values=Object.fromEntries(new FormData(f));if(registration&&!registrationToken){values.yclients_enabled=registrationMode.value==='true';if(!values.yclients_enabled)delete values.branch_id;const details=Object.fromEntries(['branch_id','branch_name','name','phone','yclients_enabled'].filter(key=>key in values).map(key=>[key,values[key]]));const result=await post('/register-branch',details);if(version!==flowVersion||finished)return;registrationToken=result.registration_token;}
    const token=registrationToken;cooldown(60);const result=await post(registration?'/register-branch/code':'/invite/'+data.token+'/code',registration?{registration_token:token}:{phone:values.phone});if(version!==flowVersion||token!==registrationToken||finished)return;received(result);code.focus();
   }catch(error){if(version!==flowVersion||finished)return;if(error.retry_after)cooldown(error.retry_after);else if(error.status===400)cooldown(0);else waiting=true;tell(error.message,true);}finally{if(version===flowVersion){sending=false;paint();if(waiting&&!finished)refreshCode();}}
  });
  const timer=setInterval(paint,250),poll=setInterval(()=>{if(waiting&&!document.hidden)refreshCode();},2000);paint();
  addEventListener('pagehide',()=>{clearInterval(timer);clearInterval(poll);});
  submit(f,'Завершить регистрацию',async values=>{if(sending||!challenge||!ready)throw new Error('Сначала запросите SMS-код и дождитесь отправки');await post(registration?'/register-branch/complete':'/invite/'+data.token+'/accept',{...values,registration_token:registrationToken,challenge_id:challenge});finished=true;clearInterval(timer);clearInterval(poll);root.replaceChildren(element('p','','Регистрация завершена. Войдите с указанным email и паролем.'));const link=element('a','btn-primary','Войти');link.href='/admin/login';root.append(link);});
 } else if(boot.kind==='preferences'){
  let prefs=data.preferences,busy=false,version=0,connecting=null;
  const channelNames={email:'Email',telegram:'Telegram Bot',max:'MAX Bot',messengers:'Мессенджеры (сообщение от администратора)',sms:'СМС',push:'Push'},icons={email:'@',telegram:'TG',max:'M',messengers:'↗',sms:'SMS',push:'◉'},cards={};
  description.textContent=prefs.name+' · '+(prefs.phone || '');root.classList.add('client-preferences');
  const intro=element('p','page-description','Выберите, где получать ссылку на пропуск. Изменения сохраняются автоматически.'),state=element('p','preference-state'),list=element('div','preference-channels');state.id='clientMailingState';state.setAttribute('role','status');root.append(intro,state,list);
  const reasonText={sms_only:'Филиал использует СМС. Вы можете включить канал «СМС».',client_id_missing:'Для этого клиента отправка через сервис филиала пока недоступна. Обратитесь к администратору.',credentials_missing:'Администратор ещё не подключил рассылку филиала.',not_configured:'Администратор ещё не подключил рассылку филиала.',chatpush_not_configured:'Администратор ещё не подключил рассылку филиала.',yclients_not_configured:'Администратор ещё не подключил рассылку филиала.',provider_unavailable:'Рассылка филиала пока недоступна.',unsupported_branch:'Рассылка филиала пока недоступна.'};
  function allowedChannels(){return prefs.allowed_channels||Object.keys(channelNames);}
  function visibleChannels(){return allowedChannels().filter(channel=>channel!=='push'||window.GateBotPWA?.canPush);}
  function channelChange(channel,enabled){
   const channels=Object.fromEntries(allowedChannels().map(key=>[key,Boolean(prefs.enabled&&prefs.channels?.[key])]));
   channels[channel]=enabled;return {enabled:enabled||prefs.enabled,channels};
  }
  async function preferencePost(path,payload){
   try{return await post(path,payload);}catch(error){
    if(error.status!==403||error.data?.code!=='csrf_expired')throw error;
    // A rejected CSRF request has not reached the mutation. Refresh the form
    // session and retry once; transport failures/unknown outcomes never retry.
    await api('/preferences/'+data.token+'/status');
    return post(path,payload);
   }
  }
  function ensureCards(){for(const [channel,title] of Object.entries(channelNames)){
   if(!visibleChannels().includes(channel)||cards[channel])continue;
   const card=element('section','preference-channel'),row=element('div','preference-channel-row'),icon=element('span','preference-channel-icon',icons[channel]),text=element('div','preference-channel-copy'),label=element('label','preference-channel-label',title),status=element('p','preference-channel-status'),input=element('input','preference-switch');
   if(channel==='push')icon.innerHTML='<svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9a6 6 0 0 1 12 0c0 7 3 7 3 9H3c0-2 3-2 3-9Zm4 12h4"/></svg>';
   input.type='checkbox';input.name=channel;input.id='clientChannel-'+channel;input.setAttribute('role','switch');label.htmlFor=input.id;status.id=input.id+'-status';input.setAttribute('aria-describedby',status.id);icon.setAttribute('aria-hidden','true');text.append(label,status);row.append(icon,text,input);card.append(row);list.append(card);cards[channel]={card,input,status};
   input.addEventListener('change',async()=>{
    if(busy){paint();return;}const requested=input.checked;
    if(channel==='push'&&requested){
     const attempt=window.GateBotPWA.enablePush();setBusy(true);++version;
     try{const result=await attempt;if(result.preferences)prefs=result.preferences;else await refresh();closeConnection();}
     catch(error){tell(error.message,true);}finally{setBusy(false);}return;
    }
    if(requested&&!prefs.connected?.[channel]){paint();openConnection(channel);return;}
    try{await save(channelChange(channel,requested));closeConnection();}catch(error){tell(error.message,true);paint();}
   });
  }}
  ensureCards();
  const connection=element('div','preference-connection');connection.hidden=true;connection.id='clientChannelConnection';list.append(connection);
  function closeConnection(){connecting=null;connection.hidden=true;connection.replaceChildren();}
  function openConnection(channel){
   connecting=channel;connection.hidden=false;connection.replaceChildren(element('h2','','Подключить '+channelNames[channel]));cards[channel].card.append(connection);
   if(channel==='email'){
    const emailForm=element('form','admin-form');connection.append(element('p','page-description','Укажите адрес. Отправим ссылку для подтверждения — после перехода по ней канал включится.'));
    const email=field(emailForm,'Ваш email','email','email',true,prefs.email||'');email.autocomplete='email';email.maxLength=254;connection.append(emailForm);
    submit(emailForm,'Получить ссылку подтверждения',async values=>{
     await save({...channelChange('email',true),email:values.email});
     setBusy(true);++version;
     try{const result=await preferencePost('/preferences/'+data.token+'/verify-email',{});tell(delivery(result));connection.querySelector('.page-description').textContent='Перейдите по ссылке из письма и вернитесь сюда. Состояние обновится автоматически.';}finally{setBusy(false);}
    });
    email.focus();
   }else if(channel==='telegram'||channel==='max'){
    connection.append(element('p','page-description','Откройте бот по персональной ссылке и нажмите «Начать». Номер телефона повторно вводить не нужно.'));
    action(connection,'Открыть '+channelNames[channel],async()=>{
     await save(channelChange(channel,true));setBusy(true);++version;
     try{const result=await preferencePost('/preferences/'+data.token+'/bot-link',{platform:channel});location.assign(result.link);}finally{setBusy(false);}
    });
   }else if(channel==='sms'){
    connection.append(element('p','page-description','Номер телефона в карточке недоступен. Обратитесь к администратору филиала.'));
   }else{
    connection.append(element('p','page-description',reasonText[prefs.messengers?.reason]||'Рассылка через сервис филиала пока недоступна для вашего профиля. Обратитесь к администратору.'));
   }
   action(connection,'Отмена',closeConnection);
  }
  const unsubscribe=action(root,'Не получать уведомления о пропуске',()=>confirmation.showModal());unsubscribe.id='unsubscribeClientNotifications';unsubscribe.classList.add('preference-unsubscribe');
  root.insertBefore(unsubscribe,list);
  const confirmation=element('dialog');confirmation.id='unsubscribeNotificationsDialog';confirmation.setAttribute('aria-labelledby','unsubscribeNotificationsHeading');const heading=element('h2','','Отключить уведомления?');heading.id='unsubscribeNotificationsHeading';confirmation.append(heading,
   element('p','','Ссылка на пропуск останется действительной. Добавьте её в закладки: перед записью на услуги пропуск будет автоматически активироваться заблаговременно. Отписка отключает только сообщения.'),
   element('p','','Чтобы снова получать уведомления, включите нужный канал на этой странице.'));
  const dialogActions=element('div','preference-dialog-actions');confirmation.append(dialogActions);action(dialogActions,'Оставить уведомления',()=>confirmation.close());action(dialogActions,'Не получать уведомления',async()=>{await save({enabled:false});closeConnection();confirmation.close();tell('Вы отписались от уведомлений. Ссылка на пропуск продолжает работать.');});root.append(confirmation);
  function setBusy(value){busy=value;if(value){for(const card of Object.values(cards))card.input.disabled=true;unsubscribe.disabled=true;}else paint();for(const node of connection.querySelectorAll('input,button'))node.disabled=value;for(const node of confirmation.querySelectorAll('button'))node.disabled=value;}
  function paint(){
   ensureCards();
   const allowed=allowedChannels(),active=Boolean(prefs.enabled&&allowed.some(channel=>prefs.channels?.[channel]&&prefs.connected?.[channel]));
   state.textContent=prefs.delivery_enabled===false?'Уведомления отключены администратором':active?'Вы получаете уведомления по включённым каналам.':'Уведомления отключены. Включите нужный канал, чтобы получать ссылку на пропуск. Ваш пропуск и персональная ссылка сохраняются.';
   for(const [channel,card] of Object.entries(cards)){
    card.card.hidden=!visibleChannels().includes(channel);
    const connected=Boolean(prefs.connected?.[channel])&&(channel!=='push'||window.GateBotPWA?.deviceSubscribed),selected=Boolean(prefs.channels?.[channel]);
    card.input.checked=Boolean(prefs.enabled&&selected&&connected&&allowed.includes(channel));card.input.disabled=busy||prefs.delivery_enabled===false||!allowed.includes(channel);
    card.status.textContent=!prefs.enabled?'Уведомления отключены':connected?(selected?'Включено':'Выключено'):(channel==='messengers'?'Сервис филиала пока недоступен':selected?'Подключение не завершено':'Включите, чтобы подключить');
    if(channel==='email'&&connected)card.status.textContent+=(prefs.email?' · '+prefs.email:'');
    if(channel==='sms'&&connected)card.status.textContent+=(prefs.phone?' · '+prefs.phone:'');
    if(channel==='push')card.status.textContent=connected?(card.input.checked?'Включено на этом устройстве':'Выключено · можно включить на этом устройстве'):'Включите уведомления на этом устройстве';
    card.card.classList.toggle('preference-channel-active',card.input.checked);
   }
   unsubscribe.hidden=!active||prefs.delivery_enabled===false;unsubscribe.disabled=busy||prefs.delivery_enabled===false;
   if(connecting&&(prefs.delivery_enabled===false||!allowed.includes(connecting)))closeConnection();
  }
  async function save(changes){
   const current=++version;setBusy(true);
   try{const payload={enabled:prefs.enabled,channels:prefs.channels,...changes};payload.channels=Object.fromEntries(Object.entries(payload.channels).filter(([channel])=>allowedChannels().includes(channel)));const result=await preferencePost('/preferences/'+data.token,payload);if(current===version){prefs=result.preferences;paint();}return result;}finally{setBusy(false);}
  }
  async function refresh(){
   if(document.hidden||busy)return;const current=++version;
   try{const result=await api('/preferences/'+data.token+'/status');if(current!==version)return;prefs=result.preferences;paint();if(connecting&&prefs.effective_channels?.[connecting])closeConnection();}catch{}
  }
  addEventListener('focus',refresh);addEventListener('pageshow',event=>{if(event.persisted)refresh();});document.addEventListener('visibilitychange',refresh);paint();
  addEventListener('gatebot:pwa-change',event=>{if(event.detail?.reason==='subscribed'&&event.detail.preferences)prefs=event.detail.preferences;paint();});
 } else if(boot.kind==='admin_preferences'){
  const prefs=data.preferences;
  if(prefs.requires_phone){
   if(boot.is_super){
    root.append(element('h2','','Привяжите телефон аккаунта'),element('p','','В каталоге GateBot выполните команду. Замените LOGIN на свой логин, а PHONE на номер с кодом страны. Затем обновите страницу.'));
    const command=element('pre');command.append(element('code','','python admin.py --link-person --username LOGIN --phone PHONE'));root.append(command);
   }else root.append(element('p','','Для настройки уведомлений обратитесь в поддержку GateBot и укажите номер телефона вашего аккаунта.'));
   return;
  }
  description.textContent=prefs.is_super?'Подключите нужные каналы и выберите события для каждого из них.':'Email берётся из вашей карточки клиента. Подключите нужные каналы и выберите события для каждого из них.';
  const channelLabels={...labels,push:'Push'};
  const channels=['email','telegram','max','push'],connections=element('div','stat-grid'),cards={},controls={};
  connections.id='adminNotificationChannels';let connectionVersion=0,pendingSaves=0;
  for(const channel of channels){
   const card=element('section','stat-card'),status=element('p'),detail=element('p','page-description');
   card.dataset.channel=channel;status.id='adminChannelStatus-'+channel;status.setAttribute('role','status');
   card.append(element('h3','',channelLabels[channel]),status,detail);connections.append(card);cards[channel]={card,status,detail};
   if(['telegram','max'].includes(channel))cards[channel].connect=action(card,'Подключить '+labels[channel],async()=>{const result=await post('/admin/api/identity/bot-link',{platform:channel});location.assign(result.link);});
  }
  const push=window.GateBotAdminPush,pushCard=cards.push;
  let pushDevice=null;
  pushCard.card.dataset.officePush='';
  const pushEnable=action(pushCard.card,'Подключить Push',async()=>{await push.connect();await refreshPush(true);});
  const pushDisable=action(pushCard.card,'Отключить на этом устройстве',async()=>{await push.disconnect();await refreshPush(true);});
  pushEnable.hidden=pushDisable.hidden=true;
  root.append(connections);
  const emailForm=element('form','admin-form notification-channel-editor');emailForm.id='adminNotificationEmailForm';emailForm.hidden=true;
  const emailInput=field(emailForm,'Email','email','email',false,prefs.email);emailInput.autocomplete='email';emailInput.maxLength=254;
  emailForm.append(element('p','form-hint',prefs.is_super?'Чтобы отключить Email, очистите поле.':'Адрес сохраняется в вашей карточке клиента. Пустое поле отключит канал Email.'));
  const emailEdit=action(cards.email.card,prefs.email?'Изменить email':'Подключить Email',()=>{emailInput.value=prefs.email||'';emailForm.hidden=false;emailEdit.hidden=true;emailInput.focus();});
  const emailSave=submit(emailForm,'Сохранить email',async values=>{
   const version=++connectionVersion;pendingSaves++;savePreferences.disabled=true;emailInput.disabled=true;emailCancel.disabled=true;
   try{
    const result=await post('/admin/api/identity/email',{email:values.email});
    if(version!==connectionVersion)return;
    Object.assign(prefs,result.preferences);emailForm.hidden=true;paintConnections();emailEdit.focus();
    tell(prefs.channels.email?'Email сохранён в карточке клиента.':'Адрес удалён. Канал Email отключён.');
   }finally{pendingSaves--;savePreferences.disabled=false;emailInput.disabled=false;emailCancel.disabled=false;}
  });
  const emailCancel=action(emailForm,'Отмена',()=>{emailForm.hidden=true;emailEdit.hidden=false;emailEdit.focus();});cards.email.card.append(emailForm);
  const f=form(),table=element('table','users-table admin-event-matrix'),head=element('tr'),eventControls={};
  f.append(element('h2','','События и каналы'),element('p','page-description','Отметьте события для ленты и выберите, куда присылать уведомления.'));
  for(const title of ['Событие','Email','Telegram','MAX','Push'])head.append(element('th','',title));table.append(head);
  for(const event of data.events){
   const row=element('tr'),eventCell=element('th');
   eventCell.scope='row';
   eventControls[event]=check(eventCell,events[event]||event,event,prefs.active_events.includes(event));
   eventControls[event].onchange=()=>paintConnections();
   row.append(eventCell);controls[event]={};
   for(const channel of channels){
    const cell=element('td'),input=check(cell,'',event+'_'+channel,prefs.events[event]?.[channel]);controls[event][channel]=input;
    cell.dataset.channel=channelLabels[channel];
    input.setAttribute('aria-label',(events[event]||event)+' '+channelLabels[channel]);input.setAttribute('aria-describedby','adminChannelStatus-'+channel);row.append(cell);
   }
   table.append(row);
  }
  const tableScroll=element('div','users-table-container');tableScroll.append(table);f.append(tableScroll);
  const unavailable=element('p','page-description');unavailable.id='adminNotificationUnavailable';unavailable.setAttribute('role','status');f.append(unavailable);
  function refreshUnavailable(){const pending=channels.filter(channel=>!prefs.channels?.[channel]&&data.events.some(event=>eventControls[event].checked&&controls[event][channel].checked));unavailable.hidden=!pending.length;unavailable.textContent=pending.length?'Для доставки через '+pending.map(channel=>channelLabels[channel]).join(', ')+' завершите подключение канала. Выбранные события сохранятся.':'';return pending.length;}
  function paintConnections(){
   for(const channel of channels){
    const connected=Boolean(prefs.channels?.[channel]),card=cards[channel];
    card.status.className='badge '+(connected?'success':'warning');
    if(channel==='push'){
     const blocked=window.Notification?.permission==='denied';
     card.status.className='badge '+(pushDevice?.connected?'success':'warning');
     card.status.textContent=pushDevice?.connected?'Уведомления подключены.':pushDevice?.unsupported?'Недоступно в этом браузере':blocked?'Заблокировано браузером':pushDevice?'Не подключено на этом устройстве':'Проверяем браузер…';
     card.detail.textContent=blocked?'Разрешите уведомления в настройках сайта.':pushDevice?.unsupported?'На iPhone откройте установленный мобильный кабинет.':connected&&!pushDevice?.connected?'Подключено другое устройство.':'';
     pushEnable.hidden=!pushDevice||pushDevice.unsupported||blocked||pushDevice.connected;
     pushDisable.hidden=!pushDevice?.connected;
    }else{
    card.status.textContent=connected?(channel==='email'?'Адрес подключён':'Аккаунт подключён'):(channel==='email'?'Адрес не указан':'Аккаунт не подключён');
    card.detail.textContent=channel==='email'?(prefs.email||'Добавьте email в карточку клиента или подключите его здесь.'):(connected?'':'Подключите бота по персональной ссылке, даже если уже пользовались им как клиент.');
    }
    card.detail.hidden=!card.detail.textContent;
    if(card.connect)card.connect.hidden=connected;
    for(const [event,row] of Object.entries(controls)){row[channel].disabled=!connected||!eventControls[event].checked;row[channel].title=connected?'':'Сначала подключите '+channelLabels[channel];}
   }
   emailEdit.textContent=prefs.email?'Изменить email':'Подключить Email';emailEdit.hidden=!emailForm.hidden;refreshUnavailable();
  }
  f.addEventListener('change',refreshUnavailable);
  const savePreferences=submit(f,'Сохранить',async()=>{
   const version=++connectionVersion;pendingSaves++;emailEdit.disabled=true;emailInput.disabled=true;emailSave.disabled=true;emailCancel.disabled=true;
   try{
    const selected=data.events.filter(event=>eventControls[event].checked);
    const matrix=Object.fromEntries(Object.entries(controls).map(([event,choices])=>[event,Object.fromEntries(
     Object.entries(choices).filter(([channel])=>channel!=='push'||prefs.channels.push||'push' in prefs.events[event]).map(([channel,input])=>[channel,input.checked]))]));
    const result=await post('/admin/api/identity/notification-settings',{events:matrix,active_events:selected});
    if(version===connectionVersion){Object.assign(prefs,result.preferences);paintConnections();}
    tell(refreshUnavailable()?'Настройки сохранены. Для выбранных неподключённых каналов нужно завершить подключение.':'Настройки сохранены');
   }finally{pendingSaves--;emailEdit.disabled=false;emailInput.disabled=false;emailSave.disabled=false;emailCancel.disabled=false;}
  });paintConnections();
  async function refreshPush(changed=false){
   pushDevice=await push.inspect();
   if(changed){
    const result=await api('/admin/api/identity/notification-settings');
    Object.assign(prefs,result.preferences);
    for(const event of data.events)controls[event].push.checked=Boolean(prefs.events[event]?.push);
   }
   paintConnections();
  }
  refreshPush().catch(error=>{pushCard.status.textContent=error.message;});
  async function refreshConnections(){
   if(document.hidden||pendingSaves)return;const version=++connectionVersion;
   try{const result=await api('/admin/api/identity/notification-settings');if(version===connectionVersion){Object.assign(prefs,result.preferences);paintConnections();}}catch{}
  }
  addEventListener('focus',refreshConnections);addEventListener('pageshow',event=>{if(event.persisted)refreshConnections();});
  const inbox=element('a','btn-action','Открыть ленту уведомлений');inbox.href=document.body.hasAttribute('data-office')?'/office/notifications':'/admin/notifications';root.append(inbox);
 } else if(boot.kind==='email_verify'){
  description.textContent='Подтвердите, что хотите получать уведомления на этот адрес.';action(root,'Подтвердить email',async()=>{await post('/verify-email/'+data.token,{});tell('Email подтверждён');});

 }
})();
