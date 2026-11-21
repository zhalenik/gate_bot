/* One subscription per browser profile for both administrator views. */
(() => {
  'use strict';
  const office = document.body.hasAttribute('data-office');
  const apiBase = office ? '/office/push/' : '/admin/api/browser-push/';
  const dismissal = 'gatebot.push.later.' + document.currentScript.dataset.owner;
  const supported = window.isSecureContext && 'serviceWorker' in navigator &&
    'Notification' in window && 'PushManager' in window;

  async function api(action, body) {
    const response = await fetch(apiBase + action, {method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},
      body:JSON.stringify(body)});
    const result = await response.json();
    if (!response.ok) throw Error(result.message || 'Не удалось настроить уведомления.');
    return result;
  }

  async function inspect() {
    if (!supported) return {unsupported:true};
    const registration = await navigator.serviceWorker.getRegistration('/office/');
    const subscription = registration && await registration.pushManager.getSubscription();
    return api('state',{endpoint:subscription?.endpoint});
  }

  async function connect() {
    // Request permission before the first async network operation to retain the user gesture.
    if (Notification.permission !== 'granted' && await Notification.requestPermission() !== 'granted') {
      throw Error('Чтобы включить Push, разрешите уведомления в настройках этого сайта.');
    }
    const registration = await navigator.serviceWorker.register('/office/sw.js',{scope:'/office/'});
    if (!registration.active) {
      const worker = registration.installing || registration.waiting;
      if (worker) await new Promise((resolve,reject) => {
        function changed() {
          if (!['activated','redundant'].includes(worker.state)) return;
          worker.removeEventListener('statechange',changed);
          if (worker.state === 'activated') resolve(); else reject(Error('Не удалось включить Push.'));
        }
        worker.addEventListener('statechange',changed); changed();
      });
    }
    let device = await registration.pushManager.getSubscription();
    if (!device) {
      const {public_key} = await api('state',{});
      const decoded = atob(public_key.replace(/-/g,'+').replace(/_/g,'/'));
      device = await registration.pushManager.subscribe({userVisibleOnly:true,
        applicationServerKey:Uint8Array.from(decoded,char => char.charCodeAt(0))});
    }
    await api('subscribe',{consent:true,subscription:device.toJSON()});
  }

  async function disconnect() {
    const registration = await navigator.serviceWorker.getRegistration('/office/');
    const device = registration && await registration.pushManager.getSubscription();
    if (device) {
      await api('unsubscribe',{endpoint:device.endpoint});
      await device.unsubscribe();
    }
    later();
  }

  function later() { try { localStorage.setItem(dismissal,String(Date.now())); } catch {} }
  function dismissed() {
    try { return Date.now() - Number(localStorage.getItem(dismissal)) < 30*86400000; } catch { return false; }
  }
  window.GateBotAdminPush = {inspect,connect,disconnect};

  const offer = document.querySelector('[data-admin-push-offer]');
  if (!offer || !supported || Notification.permission === 'denied' || dismissed() || window.top !== window) return;
  const enable = offer.querySelector('[data-push-enable]');
  offer.querySelector('[data-push-dismiss]').onclick = () => { later(); offer.hidden = true; };
  enable.onclick = async () => {
    enable.disabled = true;
    try { await connect(); offer.hidden = true; }
    catch (error) { offer.querySelector('[data-push-status]').textContent = error.message; }
    finally { enable.disabled = false; }
  };
  inspect().then(state => { offer.hidden = Boolean(state.connected); }).catch(() => {});
})();
