'use strict';
(() => {
 const form=document.getElementById('smtpForm'),notice=document.getElementById('smtpNotice');
 function tell(text,error=false){notice.hidden=false;notice.className='panel '+(error?'alert-error':'alert-success');notice.textContent=text;}
 async function request(path,data){const options={credentials:'same-origin'};if(data!==undefined){options.method='POST';options.headers={'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content};options.body=JSON.stringify(data);}const response=await fetch(path,options);let result;try{result=await response.json();}catch{throw new Error('Нет ответа сервера');}if(!response.ok)throw new Error(result.message||'Не удалось выполнить действие');return result;}
 function apply(settings){
   for(const key of ['host','port','security','username','from_name','from_email'])form.elements[key].value=settings[key]??'';
   form.elements.enabled.checked=settings.enabled;form.elements.password.value='';form.elements.clear_password.checked=false;
   document.getElementById('smtpPasswordHint').textContent=settings.has_password?'Пароль сохранён. Оставьте поле пустым, чтобы его не менять.':'Пароль пока не задан.';
   const paused=[];
   if(!settings.client_outbound_enabled)paused.push('клиентам');
   if(!settings.admin_outbound_enabled)paused.push('администраторам');
   document.getElementById('smtpState').textContent=paused.length?'Системные настройки приостанавливают отправку уведомлений '+paused.join(' и ')+'.':'';
 }
 function values(){return {enabled:form.elements.enabled.checked,host:form.elements.host.value,port:Number(form.elements.port.value),security:form.elements.security.value,username:form.elements.username.value,password:form.elements.password.value,from_name:form.elements.from_name.value,from_email:form.elements.from_email.value,clear_password:form.elements.clear_password.checked};}
 let busy=false;
 async function action(callback){if(busy)return;busy=true;form.querySelectorAll('button').forEach(b=>b.disabled=true);try{await callback();}catch(error){tell(error.message,true);}finally{busy=false;form.querySelectorAll('button').forEach(b=>b.disabled=false);}}
 form.addEventListener('submit',event=>{event.preventDefault();action(async()=>{apply((await request('/admin/api/smtp',values())).settings);tell('Настройки email сохранены');});});
 document.getElementById('smtpCheck').addEventListener('click',()=>action(async()=>{if(!form.reportValidity())return;apply((await request('/admin/api/smtp',values())).settings);const result=await request('/admin/api/smtp/check',{});tell(result.message);}));
 form.elements.security.addEventListener('change',()=>{const port=Number(form.elements.port.value);if([465,587].includes(port))form.elements.port.value=form.elements.security.value==='ssl'?'465':'587';});
 action(async()=>apply((await request('/admin/api/smtp')).settings));
})();
