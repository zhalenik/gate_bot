(() => {
  'use strict';
  function read(form){
    const fields=form.elements;
    return {fit:fields.icon_fit.value, padding:Number(fields.icon_padding.value),
      background:fields.icon_transparent.checked?'transparent':fields.icon_background.value};
  }
  function fill(form,frame={}){
    const fields=form.elements;
    fields.icon_fit.value=frame.fit||'cover';fields.icon_padding.value=frame.padding??0;
    fields.icon_transparent.checked=frame.background==='transparent';
    fields.icon_background.value=fields.icon_transparent.checked?'#ffffff':(frame.background||'#ffffff');
  }
  function preview(form,src){
    const frame=read(form), wrapper=form.querySelector('[data-icon-frame]'), img=wrapper.querySelector('img');
    wrapper.style.padding=Math.min(30,Math.max(0,frame.padding))+'%';
    wrapper.style.backgroundColor=frame.background;img.style.objectFit=frame.fit;
    form.elements.icon_background.disabled=form.elements.icon_transparent.checked;
    if(src && img.getAttribute('src')!==src)img.setAttribute('src',src);
    else if(!src)img.removeAttribute('src');
  }
  window.GateBotIconFrame={read,fill,preview};
})();
