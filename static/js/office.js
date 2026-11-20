(() => {
  'use strict';
  const installed = () => matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  let installPrompt;
  window.addEventListener('beforeinstallprompt', event => { event.preventDefault(); installPrompt = event; });
  const install = document.querySelector('[data-install-office]');
  const hideInstall = () => { if (install) install.hidden = installed(); };
  hideInstall(); window.addEventListener('appinstalled', hideInstall);
  install?.addEventListener('click', async () => {
    if (installPrompt) { await installPrompt.prompt(); installPrompt = null; }
    else document.querySelector('[data-office-install-dialog]').showModal();
  });
  for (const time of document.querySelectorAll('[data-office-date]')) {
    const date = new Date(time.dateTime);
    if (!Number.isNaN(date.getTime())) time.textContent = date.toLocaleString('ru-RU', {dateStyle:'short',timeStyle:'short'});
  }
  const worker = window.isSecureContext && 'serviceWorker' in navigator ?
    navigator.serviceWorker.register('/office/sw.js', {scope:'/office/'}).then(() => navigator.serviceWorker.ready).catch(() => null) :
    Promise.resolve(null);
  const panel = document.querySelector('[data-office-push]');
  if (!panel) return;
  const status = panel.querySelector('[data-push-status]'), fields = panel.querySelector('fieldset');
  const enable = panel.querySelector('[data-push-enable]'), disable = panel.querySelector('[data-push-disable]');
  let registration, device, key;
  const api = async (action, data) => {
    const response = await fetch('/office/push/' + action, {method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},
      body:JSON.stringify(data)});
    const result = await response.json();
    if (!response.ok) throw Error(result.message || 'Войдите в кабинет заново.');
    return result;
  };
  async function load() {
    registration = await worker;
    if (!registration || !('pushManager' in registration) || !('Notification' in window)) {
      status.textContent = 'Для Push откройте установленный кабинет в браузере с поддержкой уведомлений.'; return;
    }
    device = await registration.pushManager.getSubscription();
    const state = await api('state', {endpoint:device?.endpoint});
    key = state.public_key; fields.hidden = false;
    fields.querySelectorAll('label').forEach(label => label.remove());
    for (const [event,title] of Object.entries(state.events)) {
      const label = document.createElement('label'), input = document.createElement('input');
      input.type = 'checkbox'; input.value = event; input.checked = state.selected.includes(event);
      label.append(input, document.createTextNode(title)); fields.append(label);
    }
    status.textContent = state.connected ? 'Уведомления подключены.' :
      Notification.permission==='denied' ? 'Разрешите уведомления в настройках устройства или браузера.' : 'Выберите события, которые хотите получать.';
    enable.hidden = false; enable.textContent = state.connected ? 'Сохранить выбор' : 'Подключить Push';
    disable.hidden = !state.connected;
  }
  async function change(fn) {
    enable.disabled = disable.disabled = true;
    try { await fn(); await load(); } catch (error) { status.textContent = error.message; }
    finally { enable.disabled = disable.disabled = false; }
  }
  enable.addEventListener('click', () => change(async () => {
    const consent = Notification.permission==='granted' || await Notification.requestPermission()==='granted';
    if (!consent) throw Error('Для доставки Push разрешите уведомления на этом устройстве.');
    if (!device) {
      const raw = atob(key.replace(/-/g,'+').replace(/_/g,'/'));
      device = await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:Uint8Array.from(raw,c=>c.charCodeAt(0))});
    }
    const events = Array.from(fields.querySelectorAll('input:checked'), input => input.value);
    await api('subscribe',{consent,subscription:device.toJSON(),events});
  }));
  disable.addEventListener('click', () => change(async () => {
    if (device) { await api('unsubscribe',{endpoint:device.endpoint}); await device.unsubscribe(); }
  }));
  load().catch(error => { status.textContent = error.message; });
})();
