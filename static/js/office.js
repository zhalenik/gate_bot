(() => {
  'use strict';
  const installed = () => matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  let installPrompt;
  window.addEventListener('beforeinstallprompt', event => { event.preventDefault(); installPrompt = event; });
  const menu = document.querySelector('.office-menu');
  document.addEventListener('click', event => { if(menu?.open && !menu.contains(event.target)) menu.open=false; });
  document.addEventListener('keydown', event => {
    if(event.key==='Escape' && menu?.open){menu.open=false;menu.querySelector('summary').focus();}
  });
  const hint=document.querySelector('[data-install-hint]');
  const installButtons=document.querySelectorAll('[data-install-office]');
  const touchBrowser=navigator.userAgentData?.mobile || /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent) ||
    (navigator.platform==='MacIntel' && navigator.maxTouchPoints>1);
  function showInstall(){
    let dismissed=false;
    try { dismissed=localStorage.getItem('office.install.dismissed')==='1'; } catch {}
    if(hint) hint.hidden=installed() || !touchBrowser || dismissed;
    installButtons.forEach(button=>{button.hidden=installed();});
  }
  document.querySelector('[data-dismiss-install]')?.addEventListener('click',()=>{
    hint.hidden=true;
    try { localStorage.setItem('office.install.dismissed','1'); } catch {}
  });
  showInstall(); window.addEventListener('appinstalled',()=>{ if(hint)hint.hidden=true; installButtons.forEach(button=>{button.hidden=true;}); });
  for(const button of installButtons) button.addEventListener('click', async () => {
    if(menu) menu.open=false;
    if (installPrompt) { await installPrompt.prompt(); installPrompt = null; }
    else document.querySelector('[data-office-install-dialog]').showModal();
  });
  for (const time of document.querySelectorAll('[data-office-date]')) {
    const date = new Date(time.dateTime);
    if (!Number.isNaN(date.getTime())) time.textContent = date.toLocaleString('ru-RU', {dateStyle:'short',timeStyle:'short'});
  }
  if (window.isSecureContext && 'serviceWorker' in navigator) {
    navigator.serviceWorker.register('/office/sw.js', {scope:'/office/'}).catch(() => {});
  }
})();
