(() => {
  'use strict';
  const form = document.getElementById('appearanceForm');
  if (!form) return;
  const fields=form.elements, frame=document.getElementById('appearancePreview');
  const tab=document.getElementById('branch-tab-appearance'), status=document.getElementById('appearanceStatus');
  const globalEditor=document.getElementById('appearanceDefaults');
  const colorList=form.querySelector('[data-branch-colors]'), fileKeys=['pwa','favicon','background','card_background','barrier'];
  const fonts={system:'system-ui,sans-serif',humanist:'"Trebuchet MS",sans-serif',serif:'Georgia,serif',mono:'"Courier New",monospace',
    inter:'Inter,sans-serif',rubik:'Rubik,sans-serif',nunito:'Nunito,sans-serif',mulish:'Mulish,sans-serif'};
  let branch=globalEditor?'default':null, state=null, generation=0, loading=false, saving=false;
  let colors={}, removed=new Set(), blobs={};
  const base=()=>globalEditor?'/admin/api/palette/client-appearance':'/admin/api/branches/'+encodeURIComponent(branch)+'/appearance';
  const notice=(message,error=false)=>{status.textContent=message;status.className=error?'alert alert-error':'form-hint';};
  function clearFiles(){
    Object.values(blobs).forEach(url=>URL.revokeObjectURL(url));blobs={};removed.clear();
    fileKeys.forEach(key=>{fields[key].value='';});
  }
  function asset(key){
    const slot=key==='pwa'?'pwa_source':key;
    const inherited=state?.inherited_assets[slot] || (key==='pwa'?state?.default_icon_source:null);
    return removed.has(key)?inherited:blobs[key] || state?.assets[slot] || inherited;
  }
  function showAsset(key){
    const value=asset(key), image=form.querySelector('[data-asset-preview="'+key+'"]');
    if(key==='pwa')window.GateBotIconFrame.preview(form,value);
    else {
      if(value)image.src=value;else image.removeAttribute('src');
      image.hidden=!value;
    }
    form.querySelector('[data-reset-asset="'+key+'"]').hidden=removed.has(key)||!(blobs[key]||state?.own_assets.includes(key==='pwa'?'pwa_source':key));
  }
  function values(){
    const number=key=>fields[key].value===''?null:Number(fields[key].value);
    return {font:fields.font.value,font_size:number('font_size'),radius:number('radius'),
      button_gap:number('button_gap'),button_align:fields.button_align.value,button_icon:fields.button_icon.checked,
      title_align:fields.title_align.value,logo_position:fields.logo_position.value,logo_align:fields.logo_align.value,
      icon_frame:window.GateBotIconFrame.read(form),hide_logo:fields.hide_logo.checked,banner:fields.banner.value,banner_size:number('banner_size'),
      banner_align:fields.banner_align.value,banner_bg:fields.banner_bg.value,banner_color:fields.banner_color.value,colors};
  }
  function preview(){
    if(!state)return;
    form.querySelector('[data-logo-alignment]').hidden=fields.logo_position.value!=='above';
    showAsset('pwa');
    if(!frame.contentDocument?.body?.hasAttribute('inert'))return;
    const doc=frame.contentDocument, body=doc.body, root=doc.documentElement, draft=values();
    // The embedded document initially includes its saved styles. Removing this sheet
    // lets an unsaved reset reveal the installation palette again.
    doc.querySelector('#branchAppearanceValues')?.remove();
    const set=(key,value)=>{if(value==null)root.style.removeProperty(key);else root.style.setProperty(key,value);};
    for(const entry of state.palette)set(entry.variable,draft.colors[entry.id] || state.base_settings.colors[entry.id]);
    set('--client-font',fonts[draft.font]);body.dataset.clientFont=draft.font;
    set('--client-unit',draft.font_size==null?null:draft.font_size+'px');
    set('--client-radius',draft.radius==null?null:draft.radius+'px');
    set('--client-button-gap',draft.button_gap==null?null:draft.button_gap+'px');
    set('--client-button-align',draft.button_align);set('--client-title-align',draft.title_align);
    set('--client-logo-align',{left:'flex-start',center:'center',right:'flex-end'}[draft.logo_align]);
    body.dataset.logoPosition=draft.logo_position;body.dataset.buttonAlign=draft.button_align;
    body.toggleAttribute('data-hide-gate-icon',!draft.button_icon);
    set('--client-banner-size',draft.banner_size+'px');set('--client-banner-bg',draft.banner_bg);
    set('--client-banner-fg',draft.banner_color);set('--client-banner-align',draft.banner_align);
    body.toggleAttribute('data-client-size',draft.font_size!=null);body.toggleAttribute('data-client-radius',draft.radius!=null);
    body.toggleAttribute('data-hide-brand',draft.hide_logo);
    const paper=asset('background');body.toggleAttribute('data-client-wallpaper',Boolean(paper));
    set('--client-wallpaper',paper?'url("'+paper+'")':null);
    const card=asset('card_background');
    set('--client-card-image',card?'url("'+card+'")':null);
    const banner=doc.querySelector('[data-branch-banner]');banner.textContent=draft.banner;banner.hidden=!draft.banner;
    const barrier=doc.querySelector('.branch-barrier-image'), src=asset('barrier');
    if(src)barrier.src=src;else barrier.removeAttribute('src');
    barrier.hidden=!src;
  }
  function paintColors(){
    const query=form.querySelector('[data-color-search]').value.trim().toLowerCase();
    const group=form.querySelector('[data-color-group]').value;
    colorList.replaceChildren();
    for(const entry of state.palette){
      if(!query && group && entry.group!==group)continue;
      if(![entry.label,entry.group,entry.usage].join(' ').toLowerCase().includes(query))continue;
      const row=document.createElement('div'), label=document.createElement('label'), text=document.createElement('span');
      const hint=document.createElement('small'), input=document.createElement('input'), alpha=document.createElement('input');
      const reset=document.createElement('button'), value=colors[entry.id] || state.base_settings.colors[entry.id] || entry.default;
      text.textContent=entry.label;hint.textContent=entry.group+' · '+entry.usage;text.append(hint);
      input.type='color';input.value=value.slice(0,7);
      alpha.type='number';alpha.min='0';alpha.max='100';alpha.value=Math.round(parseInt(value.slice(7)||'ff',16)*100/255);
      alpha.setAttribute('aria-label','Непрозрачность, % · '+entry.label);alpha.title='Непрозрачность, %';alpha.style.width='5em';
      reset.type='button';reset.textContent='Сброс';reset.hidden=!Object.hasOwn(colors,entry.id);
      reset.onclick=()=>{delete colors[entry.id];paintColors();preview();};
      const change=()=>{
        const opacity=Math.max(0, Math.min(100,Number(alpha.value)));
        colors[entry.id]=input.value+(opacity<100?Math.round(opacity*255/100).toString(16).padStart(2,'0'):'');
        reset.hidden=false;preview();
      };
      input.oninput=change;alpha.oninput=change;
      label.append(input,text);row.className='appearance-color';row.append(label,alpha,reset);colorList.append(row);
    }
    if(!colorList.childElementCount)colorList.textContent='Подходящих цветов нет';
  }
  function paint(){
    clearFiles();const current=state.settings;
    colors=Object.fromEntries(Object.entries(current.colors).filter(([key,v])=>v!==state.base_settings.colors[key]));
    for(const key of ['font','font_size','radius','button_gap','button_align','title_align','logo_position','logo_align','banner','banner_size','banner_align','banner_bg','banner_color'])fields[key].value=current[key]??'';
    fields.hide_logo.checked=current.hide_logo;fields.button_icon.checked=current.button_icon;
    window.GateBotIconFrame.fill(form,current.icon_frame);
    const groups=form.querySelector('[data-color-group]');
    const selectedGroup=groups.value || 'Основная палитра';
    groups.replaceChildren(...Array.from(new Set(state.palette.map(row=>row.group)),name=>new Option(name,name)),new Option('Все группы',''));
    groups.value=selectedGroup;
    fileKeys.forEach(showAsset);paintColors();preview();
  }
  async function load(){
    if(!branch||state||loading||tab?.hidden)return;
    const serial=generation;loading=true;notice('Загрузка…');
    try{
      const response=await fetch(base(),{cache:'no-store'}), data=await response.json();
      if(serial!==generation)return;
      if(!response.ok)throw Error(data.message || 'Не удалось загрузить оформление');
      state=data;paint();fields[0].disabled=false;
      frame.src=globalEditor?'/admin/palette/client-appearance-preview':'/admin/branches/'+encodeURIComponent(branch)+'/appearance-preview';notice('');
    }catch(error){if(serial===generation)notice(error.message,true);}
    finally{if(serial===generation)loading=false;}
  }
  document.addEventListener('gatebot:branch-settings-loaded',event=>{
    if(globalEditor)return;
    const next=event.detail.personalization?String(event.detail.branchId):null;
    if(next===branch)return;
    ++generation;branch=next;state=null;loading=false;saving=false;clearFiles();
    fields[0].disabled=true;colorList.replaceChildren();frame.removeAttribute('src');notice('');
    if(tab.getAttribute('aria-selected')==='true')load();
  });
  if(globalEditor)globalEditor.addEventListener('toggle',()=>{if(globalEditor.open)load();});
  else {
    new MutationObserver(()=>{if(tab.getAttribute('aria-selected')==='true')load();}).observe(tab,{attributes:true,attributeFilter:['aria-selected']});
    tab.addEventListener('click',load);
  }
  frame.addEventListener('load',preview);form.addEventListener('input',preview);
  form.querySelector('[data-color-search]').oninput=()=>{if(state)paintColors();};
  form.querySelector('[data-color-group]').onchange=()=>{if(state)paintColors();};
  for(const key of fileKeys){
    fields[key].onchange=()=>{
      const file=fields[key].files[0];
      if(blobs[key])URL.revokeObjectURL(blobs[key]);delete blobs[key];
      if(file && file.size>5*1024*1024){fields[key].value='';notice('Выберите изображение до 5 МБ',true);}
      else if(file){blobs[key]=URL.createObjectURL(file);removed.delete(key);}
      showAsset(key);preview();
    };
    form.querySelector('[data-reset-asset="'+key+'"]').onclick=()=>{
      if(blobs[key])URL.revokeObjectURL(blobs[key]);delete blobs[key];
      fields[key].value='';removed.add(key);showAsset(key);preview();
      if(key==='pwa'){window.GateBotIconFrame.fill(form,state.base_settings.icon_frame);showAsset(key);}
    };
  }
  form.onsubmit=async event=>{
    event.preventDefault();if(!state||saving)return;
    const serial=generation, body=new FormData();
    body.append('appearance',JSON.stringify({revision:state.revision,base_revision:state.base_revision,settings:values(),remove:[...removed]}));
    for(const key of fileKeys){const file=fields[key].files[0];if(file)body.append(key,file);}
    saving=true;fields[0].disabled=true;
    try{
      const response=await fetch(base(),{method:'POST',body,headers:{'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content}});
      const reply=await response.json();
      if(serial!==generation)return;
      if(!response.ok)throw Error(reply.message || 'Не удалось сохранить оформление');
      Object.assign(state,reply);paint();notice('Оформление сохранено');
    }catch(error){if(serial===generation)notice(error.message,true);}
    finally{if(serial===generation){saving=false;fields[0].disabled=false;}}
  };
})();
