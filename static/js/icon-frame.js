(() => {
  'use strict';
  function read(form) {
    const f=form.elements;
    return {fit:f.icon_fit.value, padding:Number(f.icon_padding.value), zoom:Number(f.icon_zoom.value),
      x:Number(f.icon_x.value), y:Number(f.icon_y.value), background:f.icon_transparent.checked?'transparent':f.icon_background.value};
  }
  function fill(form, frame={}) {
    const f=form.elements;
    for(const [key, fallback] of Object.entries({fit:'cover',padding:0,zoom:100,x:50,y:50})) f['icon_'+key].value=frame[key]??fallback;
    f.icon_transparent.checked=frame.background==='transparent';
    f.icon_background.value=f.icon_transparent.checked?'#ffffff':(frame.background||'#ffffff');
  }
  function bindDrag(form, wrapper, viewport, img) {
    let drag;
    wrapper.addEventListener('pointerdown',event=>{
      if(event.button!==0 || !img.naturalWidth) return;
      const box=viewport.getBoundingClientRect(), draw=img.getBoundingClientRect();
      drag={frame:read(form), px:event.clientX, py:event.clientY, dx:box.width-draw.width, dy:box.height-draw.height};
      wrapper.setPointerCapture(event.pointerId);
    });
    wrapper.addEventListener('pointermove',event=>{
      if(!drag || !wrapper.hasPointerCapture(event.pointerId))return;
      const shift=(value,delta,space)=>Math.min(100,Math.max(0,Math.round(value+delta*100/space)));
      if(Math.abs(drag.dx)>0.5)form.elements.icon_x.value=shift(drag.frame.x,event.clientX-drag.px,drag.dx);
      if(Math.abs(drag.dy)>0.5)form.elements.icon_y.value=shift(drag.frame.y,event.clientY-drag.py,drag.dy);
      form.elements.icon_x.dispatchEvent(new Event('input',{bubbles:true}));
    });
    wrapper.addEventListener('lostpointercapture',()=>{drag=null;});
    img.addEventListener('load',()=>layout(form));
    wrapper.dataset.bound='true';
  }
  function layout(form) {
    const frame=read(form), wrapper=form.querySelector('[data-icon-frame]'), viewport=wrapper.querySelector('[data-crop-viewport]'), img=viewport.querySelector('img');
    const ratio=img.naturalWidth && img.naturalHeight ? img.naturalWidth/img.naturalHeight : 1;
    const aspect=wrapper.parentElement.dataset.aspect==='source'?ratio:1;
    wrapper.parentElement.style.aspectRatio=String(aspect);
    // Limit extremely tall logo previews without changing their aspect ratio.
    wrapper.parentElement.style.width='min(140px, '+(180*aspect)+'px)';
    viewport.style.inset=frame.padding+'%';
    wrapper.style.backgroundColor=frame.background;
    const relative=ratio/aspect, scale=(frame.fit==='cover'?Math.max:Math.min)(1,1/relative)*frame.zoom;
    const w=relative*scale, h=scale;
    Object.assign(img.style,{width:w+'%',height:h+'%',left:(100-w)*frame.x/100+'%',top:(100-h)*frame.y/100+'%',objectFit:frame.fit});
    form.elements.icon_background.disabled=form.elements.icon_transparent.checked;
    form.querySelector('[data-zoom-label]').textContent=frame.zoom+' %';
    if(!wrapper.dataset.bound)bindDrag(form,wrapper,viewport,img);
  }
  function preview(form,src){
    const img=form.querySelector('[data-icon-frame] img');
    if(src && img.getAttribute('src')!==src) img.src=src;
    else if(!src) img.removeAttribute('src');
    layout(form);
  }
  window.GateBotIconFrame={read,fill,preview};
})();
