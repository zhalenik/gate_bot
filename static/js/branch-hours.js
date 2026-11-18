'use strict';
(() => {
  const settingsForm=document.getElementById('branchSettingsForm');if(!settingsForm)return;
  const el=(tag,text='',cls='')=>{const node=document.createElement(tag);node.textContent=text;node.className=cls;return node;};
  const panel=el('section','','panel branch-hours-panel');panel.id='branchHoursPanel';panel.hidden=true;
  panel.append(el('h2','Время доступа по пропускам'),el('p','Расписание действует для всех пропусков, включая постоянные. Оно не меняет сами пропуска и не запускает ежедневные уведомления.','page-description'));
  const form=el('form','','admin-form'),label=el('label','Ограничивать доступ по расписанию','checkbox-label'),enabled=el('input');enabled.type='checkbox';enabled.id='branchHoursEnabled';label.prepend(enabled);form.append(label);
  const timezone=el('p','','form-hint');form.append(timezone);
  const days=el('div','','branch-hours-days');form.append(days);
  const hint=el('p','Время в часовом поясе филиала. Конец раньше начала — доступ до следующего дня. 00:00–24:00 означает весь день; день без интервалов закрыт.','form-hint');form.append(hint);
  const status=el('p','','form-hint');status.setAttribute('role','status');
  const save=el('button','Сохранить расписание','btn-primary');save.type='submit';form.append(save,status);panel.append(form);settingsForm.after(panel);
  const names=['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье'];let branch=null,generation=0,busy=false,dirty=false,loaded=false;
  const rows=[];
  function interval(row,value={start:'09:00',end:'18:00'}){
    const line=el('div','','branch-hours-interval');
    for(const [key,title] of [['start','Начало'],['end','Конец']]){const label=el('label',title),input=el('input');input.type='text';input.inputMode='numeric';input.placeholder='ЧЧ:ММ';input.pattern=key==='end'?'([01][0-9]|2[0-3]):[0-5][0-9]|24:00':'([01][0-9]|2[0-3]):[0-5][0-9]';input.maxLength=5;input.required=true;input.value=value[key];input.dataset.boundary=key;input.setAttribute('aria-label',names[row.index]+' — '+title.toLowerCase());label.append(input);line.append(label);}
    const remove=el('button','Убрать','btn-action');remove.type='button';remove.addEventListener('click',()=>{line.remove();dirty=true;update();});line.append(remove);row.windows.append(line);update();
  }
  for(let index=0;index<7;index++){
    const row=el('section','','branch-hours-day'),heading=el('h3',names[index]),windows=el('div','','branch-hours-windows'),empty=el('p','Нет доступа в этот день','form-hint'),actions=el('div','','action-buttons');
    const add=el('button','Добавить интервал','btn-action'),all=el('button','Весь день','btn-action');add.type=all.type='button';actions.append(add,all);row.append(heading,windows,empty,actions);days.append(row);const model={index,row,windows,empty,add,all};rows.push(model);
    add.addEventListener('click',()=>{dirty=true;interval(model);});all.addEventListener('click',()=>{dirty=true;windows.replaceChildren();interval(model,{start:'00:00',end:'24:00'});});
  }
  function update(){for(const row of rows){row.empty.hidden=row.windows.children.length>0;row.add.disabled=busy||!loaded||!enabled.checked||row.windows.children.length>=4;row.all.disabled=busy||!loaded||!enabled.checked;for(const node of row.windows.querySelectorAll('input,button'))node.disabled=busy||!loaded||!enabled.checked;}days.hidden=!enabled.checked;hint.hidden=!enabled.checked;save.disabled=busy||!loaded;enabled.disabled=busy||!loaded;}
  function paint(data){enabled.checked=data.schedule.enabled;timezone.textContent='Часовой пояс: '+data.timezone;for(const row of rows){row.windows.replaceChildren();for(const value of data.schedule.days[String(row.index)]||[])interval(row,value);}dirty=false;update();}
  async function load(id){const preserve=dirty&&branch===String(id||'');branch=String(id||'');const revision=++generation;panel.hidden=!branch;loaded=false;if(!branch)return;busy=true;update();try{const data=await api('/admin/api/branches/'+encodeURIComponent(branch)+'/access-hours');if(revision!==generation)return;loaded=true;if(preserve)timezone.textContent='Часовой пояс: '+data.timezone;else paint(data);status.textContent=preserve?'В расписании есть несохранённые изменения.':data.state.open?'Сейчас доступ разрешён расписанием.':'Сейчас доступ закрыт расписанием.';}catch(error){if(revision===generation)status.textContent=error.message;}finally{if(revision===generation){busy=false;update();}}}
  enabled.addEventListener('change',()=>{dirty=true;update();});form.addEventListener('input',()=>{dirty=true;});
  form.addEventListener('submit',async event=>{event.preventDefault();if(busy||!branch)return;const selected=branch,revision=generation;busy=true;update();const schedule={enabled:enabled.checked,days:{}};for(const row of rows)schedule.days[String(row.index)]=[...row.windows.children].map(line=>({start:line.querySelector('[data-boundary=start]').value,end:line.querySelector('[data-boundary=end]').value}));try{const data=await post('/admin/api/branches/'+encodeURIComponent(selected)+'/access-hours',{schedule});if(revision!==generation)return;paint(data);status.textContent='Расписание сохранено. Пропуска и уведомления не изменены.';}catch(error){if(revision===generation)status.textContent=error.message;}finally{if(revision===generation){busy=false;update();}}});
  document.addEventListener('gatebot:branch-settings-loaded',event=>load(event.detail.branchId));
  document.getElementById('newBranchBtn')?.addEventListener('click',()=>{generation++;branch=null;panel.hidden=true;});
  // The normal settings loader is asynchronous; handle an already-filled page.
  if(document.getElementById('settingsBranchId')?.value)load(document.getElementById('settingsBranchId').value);
})();
