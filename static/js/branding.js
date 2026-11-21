'use strict';
(() => {
  const panel = document.getElementById('brandingPanel'); if (!panel) return;
  const notice = document.getElementById('brandingNotice'); let state = null, busy = false;
  const drafts=new Map(), forms=panel.querySelectorAll('[data-branding-kind]');
  function releaseDraft(form) {
    if(drafts.has(form))URL.revokeObjectURL(drafts.get(form));
    drafts.delete(form);
  }
  function preview(form) {
    window.GateBotIconFrame.preview(form,drafts.get(form)||state?.[form.dataset.brandingKind+'_source_url']);
  }
  const notify = (text, error = false) => {notice.textContent = text; notice.hidden = !text; notice.classList.toggle('is-error',error);};
  function render() {
    panel.querySelectorAll('button').forEach(button => button.disabled = busy || !state || button.hasAttribute('data-branding-reset') && !state[button.closest('form').dataset.brandingKind + '_custom']);
    if (!state) return;
    document.getElementById('brandingLogoPreview').src = state.logo_url;
    document.getElementById('brandingIconPreview').src=state.icon_url;
    forms.forEach(preview);
    document.querySelectorAll('[data-brand-logo]').forEach(image => image.src = state.logo_url);
    document.querySelector('link[rel=icon]').href = state.icon_url;
    document.querySelector('link[rel=apple-touch-icon]').href = state.apple_icon_url;
  }
  async function request(path, body) {
    const headers = {'X-CSRF-Token':document.querySelector('meta[name=csrf-token]').content};
    const multipart = body instanceof FormData;
    if (body && !multipart) headers['Content-Type'] = 'application/json';
    const response = await fetch(path,{method:body ? 'POST' : 'GET',credentials:'same-origin',headers,body:body ? multipart ? body : JSON.stringify(body) : undefined});
    let data; try {data = await response.json();} catch {throw Error('Не удалось сохранить изображение. Обновите страницу.');}
    if (!response.ok) throw Error(data.message || 'Не удалось сохранить изображение');
    return data;
  }
  async function save(form, reset) {
    if (busy || !state) return; busy = true; render(); notify('');
    try {
      const kind = form.dataset.brandingKind, data = reset ? {revision:state.revision} : new FormData(form);
      if (!reset) data.append('revision',state.revision);
      state = await request('/admin/api/branding/'+kind+(reset ? '/reset' : ''),data);
      form.reset();
      releaseDraft(form);
      window.GateBotIconFrame.fill(form,state[kind+'_frame']);
      if(kind==='icon')document.dispatchEvent(new CustomEvent('gatebot:branding-icon-changed',{detail:state}));
      notify(reset ? 'Стандартное изображение восстановлено.' : 'Оформление сохранено.');
    } catch (error) {notify(error.message,true);} finally {busy = false; render();}
  }
  forms.forEach(form => {
    form.elements.image.addEventListener('change',()=>{
      releaseDraft(form);
      const file=form.elements.image.files[0];
      if(file)drafts.set(form,URL.createObjectURL(file));
      preview(form);
    });
    form.addEventListener('input',()=>preview(form));
    form.addEventListener('submit',event => {event.preventDefault(); save(form,false);});
    form.querySelector('[data-branding-reset]').addEventListener('click',() => save(form,true));
  });
  request('/admin/api/branding').then(data => {
    state=data;
    forms.forEach(form=>window.GateBotIconFrame.fill(form,state[form.dataset.brandingKind+'_frame']));
    render();
  }).catch(error => notify(error.message,true));
})();
