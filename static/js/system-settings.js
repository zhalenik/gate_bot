'use strict';
(() => {
 const form=document.getElementById('systemSettingsForm'),groups=document.getElementById('systemSettingsGroups'),notice=document.getElementById('systemSettingsNotice');
 const save=document.getElementById('systemSettingsSave'),resetAll=document.getElementById('systemSettingsReset'),search=document.getElementById('systemSettingsSearch');
 const restart=document.getElementById('systemSettingsRestart');
 const fields=new Map();let snapshot=null,busy=false;
 const node=(tag,text='',className='')=>{const value=document.createElement(tag);value.textContent=text;value.className=className;return value;};
 function tell(message,error=false){notice.hidden=false;notice.className='alert '+(error?'alert-error':'alert-success');notice.textContent=message;}
 async function api(data){const options={credentials:'same-origin',cache:'no-store'};if(data!==undefined){options.method='POST';options.headers={'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name=csrf-token]').content};options.body=JSON.stringify(data);}const response=await fetch('/admin/api/system-settings',options);let result;try{result=await response.json();}catch{throw Error('Не удалось прочитать настройки');}if(!response.ok)throw Error(result.message||'Не удалось сохранить настройки');return result.settings;}
 function display(value){return typeof value==='boolean'?(value?'Включено':'Выключено'):String(value??'')||'Не задано';}
 function paint(settings){
  snapshot=settings;fields.clear();
  const extras=[...document.querySelectorAll('[data-settings-extra], #modemSettings')];
  const expanded=new Set([...groups.querySelectorAll('details[open]')].map(section=>section.dataset.group||section.dataset.block));
  if (!groups.children.length) expanded.add('delivery');
  groups.replaceChildren();const outer={},containers={};
  for(const key of settings.group_order){
   const section=node('details','','panel system-settings-group');
   section.dataset.group=key;section.open=expanded.has(key);
   section.append(node('summary',settings.groups[key]));groups.append(section);outer[key]=section;
  }
  for(const [key,title] of Object.entries(settings.blocks)){
   const block=node('details','','panel system-settings-block'),body=node('div','','system-settings-grid');
   block.dataset.block='block-'+key;block.open=expanded.has(block.dataset.block);
   block.append(node('summary',title),body);outer[settings.block_groups[key]].append(block);containers[key]=body;
  }
  for(const extra of extras)outer[extra.dataset.settingsExtra || 'hardware'].append(extra);
  for(const field of settings.fields){
   const row=node('div','','form-group system-settings-field');row.dataset.key=field.key;
   const invalidTyped=!!field.validation_error&&['bool','int','float'].includes(field.kind);
   const label=node('label',field.label),input=document.createElement(field.kind==='select'?'select':field.kind==='template'?'textarea':'input');input.name=field.key;input.setAttribute('form',form.id);input.id='setting-'+field.key;label.htmlFor=input.id;
   if(field.kind==='select'){if(field.validation_error){const previous=node('option','Текущее значение: '+display(field.value));previous.value=field.value;input.append(previous);}for(const value of field.options){const option=node('option',value);option.value=value;input.append(option);}input.value=field.value;}
   else if(field.kind==='bool'&&!invalidTyped){input.type='checkbox';input.checked=field.value;label.className='checkbox-label';}
   else if(field.kind==='template'){input.value=field.value;input.rows=3;input.maxLength=1024;}
   else{input.type=field.secret?'password':invalidTyped?'text':['int','float'].includes(field.kind)?'number':field.kind==='url'||field.kind==='https_url'?'url':'text';input.value=field.secret?'':field.value??'';input.autocomplete=field.secret?'new-password':'off';input.maxLength=field.secret?8192:1024;if(field.minimum!==undefined)input.min=field.minimum;if(field.maximum!==undefined)input.max=field.maximum;if(field.kind==='float')input.step='any';}
   label.append(input);row.append(label,node('small',field.key));
   if(field.help)row.append(node('p',field.help,'form-hint'));
   if(field.validation_error){const error=node('p',field.validation_error,'alert alert-error');error.setAttribute('role','alert');row.append(error);input.setAttribute('aria-invalid','true');const block=containers[field.group].parentElement;block.open=true;block.parentElement.open=true;}
   else if(field.active_validation_error)row.append(node('p','Параметр текущего запуска требует перезапуска после исправления: '+field.active_validation_error,'form-hint'));
   const source=node('small',(field.source==='saved'?'Сохранено в GateBot':field.source==='environment'?'Из окружения / .env':'Значение по умолчанию')+(field.restart_required?' · Ожидает перезапуска':''));row.append(source);
   if(field.secret)row.append(node('p',field.configured?'Секрет задан. Пустое поле сохранит его без изменений.':'Секрет не задан.','form-hint'));
   else if(field.restart_required)row.append(node('p','Сейчас: '+display(field.active_value),'form-hint'));
   const controls=node('div','','action-buttons'),reset=node('button','Из окружения','btn-action');reset.type='button';reset.disabled=!field.overridden;reset.title='Удалить только сохранённое переопределение этого параметра';controls.append(reset);
   const model={field,row,input,reset,resetPending:false,clear:null};
   if(field.secret){const wrap=node('label','Очистить секрет','checkbox-label'),clear=node('input');clear.type='checkbox';clear.setAttribute('aria-label','Очистить '+field.label);wrap.prepend(clear);controls.append(wrap);model.clear=clear;clear.addEventListener('change',()=>{model.resetPending=false;input.disabled=clear.checked;reset.textContent='Из окружения';});}
   reset.addEventListener('click',()=>{model.resetPending=!model.resetPending;reset.textContent=model.resetPending?'Отменить возврат':'Из окружения';input.disabled=model.resetPending;if(model.clear){model.clear.checked=false;model.clear.disabled=model.resetPending;}source.textContent=model.resetPending?'После сохранения: значение окружения / .env':(field.overridden?'Сохранено в GateBot':'Из окружения / .env');});
   row.append(controls);containers[field.group].append(row);fields.set(field.key,model);
  }
  document.getElementById('systemSettingsState').textContent=settings.invalid_keys?.length?'Проверьте параметры: '+settings.invalid_keys.length+'. Исправьте отмеченные поля и сохраните.':settings.restart_required?'Сохранены изменения: '+settings.restart_keys.length+'. Для применения перезапустите сервис.':'Сохранённые параметры соответствуют текущему запуску.';
  save.disabled=false;restart.disabled=false;resetAll.disabled=!settings.fields.some(field=>field.overridden);filter();
 }
 function filter(){
  const query=search.value.trim().toLocaleLowerCase();
  for(const section of groups.children){
   const matchesGroup=section.querySelector('summary').textContent.toLocaleLowerCase().includes(query);
   for(const block of section.querySelectorAll(':scope > details')){
    const matchesBlock=matchesGroup || block.querySelector('summary').textContent.toLocaleLowerCase().includes(query);
    if(block.dataset.block){
     for(const row of block.querySelectorAll('.system-settings-field')){
      const {field}=fields.get(row.dataset.key);
      row.hidden=!matchesBlock && ![field.key,field.label,field.help].join(' ').toLocaleLowerCase().includes(query);
     }
     block.hidden=!block.querySelector('.system-settings-field:not([hidden])');
     if(query && !block.hidden) block.open=true;
    }else{
     block.hidden=!matchesGroup && !block.textContent.toLocaleLowerCase().includes(query);
    }
   }
   section.hidden=![...section.children].some(el=>el.tagName==='DETAILS'&&!el.hidden);
   if(query && !section.hidden) section.open=true;
  }
 }
 search.addEventListener('input',filter);
 function changes(){const payload={revision:snapshot.revision,values:{},reset:[],clear:[]};for(const [key,model] of fields){if(model.resetPending){payload.reset.push(key);continue;}if(model.clear?.checked){payload.clear.push(key);continue;}const value=model.input.type==='checkbox'?model.input.checked:model.input.type==='number'?Number(model.input.value):model.input.value;if(model.field.secret){if(value)payload.values[key]=value;}else if(value!==model.field.value)payload.values[key]=value;}return payload;}
 async function perform(callback){if(busy)return;busy=true;save.disabled=true;resetAll.disabled=true;try{await callback();}catch(error){tell(error.message,true);}finally{busy=false;if(snapshot){save.disabled=false;resetAll.disabled=!snapshot.fields.some(field=>field.overridden);}}}
 form.addEventListener('submit',event=>{
  event.preventDefault();const reboot=event.submitter===restart;
  if(reboot&&!confirm('Сохранить настройки и перезапустить GateBot? Во время перезапуска сервис будет кратковременно недоступен.'))return;
  perform(async()=>{
   const modemsSaved=await document.getElementById('modemSettingsForm')?.saveChanges?.();
   try{paint(await api(changes()));}
   catch(error){if(modemsSaved)throw Error('Список модемов сохранён. '+error.message);throw error;}
   if(!reboot){tell(snapshot.restart_required?'Настройки сохранены. Изменения применятся после перезапуска сервиса.':'Настройки сохранены.');return;}
   const result=await fetch('/admin/api/system-settings/restart',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name=csrf-token]').content},body:JSON.stringify({revision:snapshot.revision})});
   const reply=await result.json();tell(reply.message,!result.ok);
  });
 });
 resetAll.addEventListener('click',()=>{if(!confirm('Вернуть все системные параметры к значениям окружения / .env после перезапуска? Настройки SMTP сохранятся.'))return;perform(async()=>{paint(await api({revision:snapshot.revision,reset:snapshot.fields.filter(field=>field.overridden).map(field=>field.key)}));tell('Возврат к окружению сохранён. Перезапустите сервис для применения.');});});
 const compose=document.getElementById('composePreview'), output=document.getElementById('composeText');
 compose.addEventListener('submit',event=>{
  event.preventDefault();
  perform(async()=>{
   const response=await fetch(compose.action+'?'+new URLSearchParams(new FormData(compose)),{cache:'no-store'});
   if(!response.ok){output.value='';output.parentElement.hidden=true;throw Error((await response.json()).message||'Не удалось сформировать YAML');}
   output.value=await response.text();output.parentElement.hidden=false;
  });
 });
 const password=document.getElementById('transferPassword'),file=document.getElementById('settingsImportFile'),restore=document.getElementById('settingsImport');
 async function transfer(operation,data){
  const response=await fetch('/admin/api/system-settings/'+operation,{method:'POST',cache:'no-store',
   headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name=csrf-token]').content},
   body:JSON.stringify({...data,password:password.value})});
  if(!response.ok)throw Error((await response.json()).message||'Не удалось обработать файл настроек');
  return response;
 }
 function passwordReady(){password.required=true;return password.reportValidity();}
 document.getElementById('settingsExport').onclick=()=>{
  if(!passwordReady())return;
  perform(async()=>{
   const response=await transfer('export',{});
   const url=URL.createObjectURL(await response.blob()),download=document.createElement('a');
   download.href=url;download.download='gatebot-settings.json';download.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
   password.value='';tell('Файл настроек сформирован');
  });
 };
 file.onchange=()=>{restore.disabled=!file.files.length;};
 restore.onclick=()=>{
  if(!snapshot||!file.files.length||!passwordReady()||!confirm('Заменить системные параметры, модемы и SMTP настройками из этого файла?'))return;
  perform(async()=>{
   if(file.files[0].size>1024*1024)throw Error('Файл должен быть меньше 1 МБ');
   let documentData;
   try{documentData=JSON.parse(await file.files[0].text());}catch{throw Error('Не удалось прочитать файл JSON');}
   await transfer('import',{revision:snapshot.revision,document:documentData});
   password.value='';location.reload();
  });
 };
 perform(async()=>paint(await api()));
})();
