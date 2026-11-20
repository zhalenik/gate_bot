'use strict';
(() => {
  const section = document.getElementById('modemSettings');
  const form = document.getElementById('modemSettingsForm');
  const cards = document.getElementById('modemCards');
  const add = document.getElementById('modemAdd'), save = document.getElementById('modemSave');
  const notice = document.getElementById('modemSettingsNotice');
  const url = '/admin/api/system-settings/modems';
  let revision = null, snapshot = {}, baseline = '', loading = false, saving = null;
  async function request(path, data) {
    const options = {credentials:'same-origin', cache:'no-store'};
    if (data !== undefined) {
      options.method = 'POST';
      options.headers = {'Content-Type':'application/json', 'X-CSRF-Token':document.querySelector('meta[name=csrf-token]').content};
      options.body = JSON.stringify(data);
    }
    const response = await fetch(path, options);
    const result = await response.json();
    if (!response.ok) throw Error(result.message || 'Не удалось прочитать настройки модемов');
    return result;
  }
  function values(card) {
    const row = {id:card.dataset.id};
    for (const input of card.querySelectorAll('input[name]')) {
      row[input.name] = input.type === 'checkbox' ? input.checked : input.type === 'number' ? input.valueAsNumber : input.value.trim();
    }
    return row;
  }
  function describe(status) {
    if (status.state === 'busy') return '◷ Модем занят';
    if (status.state === 'error') return '⚠ Нет связи с модемом · ' + (status.error_code || 'ошибка AT');
    if (status.state !== 'ok') return '○ Ожидание проверки';
    const online = ['home','roaming'].includes(status.registration);
    return [online ? '● ' + (status.operator || 'В сети') : '○ Модем отвечает · сеть не зарегистрирована',
      status.registration === 'roaming' ? 'роуминг' : '', status.signal_dbm != null ? status.signal_dbm + ' dBm' : ''].filter(Boolean).join(' · ');
  }
  function status(card) {
    const row = values(card);
    const active = snapshot.active?.find(m => m.id === row.id);
    const device = snapshot.devices?.find(m => m.id === row.id);
    const applied = active && Object.keys(row).every(k => row[k] === active[k]);
    card.querySelector('legend').textContent = row.name || 'Новый модем';
    const probe = card.querySelector('.modem-probe');
    probe.disabled = !applied || !row.port || !snapshot.online || !!device?.busy || card.dataset.probing === 'yes';
    if (card.dataset.probing === 'yes') return;
    card.querySelector('.modem-health').textContent = !applied ? '○ Применится после сохранения и перезапуска'
      : !snapshot.online ? '○ Модуль связи выключен' : describe(device?.busy ? {state:'busy'} : (device?.status || {}));
  }
  function render(row) {
    const card = document.getElementById('modemCard').content.firstElementChild.cloneNode(true);
    card.dataset.id = row.id;
    for (const input of card.querySelectorAll('input[name]')) {
      if (input.type === 'checkbox') input.checked = row[input.name];
      else input.value = row[input.name];
    }
    card.addEventListener('input', () => status(card));
    card.querySelector('.modem-remove').onclick = () => {card.remove(); notice.textContent = 'Сохраните список, чтобы применить изменения.';};
    card.querySelector('.modem-probe').onclick = async () => {
      card.dataset.probing = 'yes'; status(card);
      const health = card.querySelector('.modem-health');
      health.textContent = 'Проверяем AT-соединение, SIM и регистрацию в сети…';
      try {
        const {result} = await request(url + '/' + row.id + '/probe', {});
        const device = snapshot.devices.find(m => m.id === row.id);
        if (device) device.status = result;
        health.textContent = describe(result);
      } catch (error) {notice.textContent = error.message;}
      finally {delete card.dataset.probing; status(card);}
    };
    cards.append(card); status(card);
    return card;
  }
  async function refresh() {
    if (loading || !section.open || document.hidden) return;
    loading = true;
    try {
      snapshot = await request(url);
      if (revision === null) {
        revision = snapshot.revision;
        snapshot.modems.forEach(render);
        baseline = JSON.stringify(Array.from(cards.children, values));
        add.disabled = save.disabled = false;
      }
      Array.from(cards.children).forEach(status);
    } catch (error) {notice.textContent = error.message;}
    finally {loading = false;}
  }
  section.addEventListener('toggle', refresh);
  setInterval(refresh, 10000);
  refresh();
  add.onclick = () => {
    const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2,'0')).join('');
    render({id,name:'SIM800C ' + (cards.children.length+1),port:'',host_device:'',baudrate:9600,command_timeout:5,
      call_priority:1,sms_priority:1,call_enabled:true,sms_enabled:true}).querySelector('input').focus();
  };
  form.saveChanges = async () => {
    if (saving) {await saving; return true;}
    const modems = Array.from(cards.children, values), serialized = JSON.stringify(modems);
    if (revision === null || baseline === serialized) return;
    if (!form.checkValidity()) {
      section.open = true; form.reportValidity();
      throw Error('Заполните параметры модема');
    }
    save.disabled = true;
    saving = request(url, {revision, modems});
    try {
      const data = await saving;
      revision = data.revision; baseline = serialized;
      notice.textContent = 'Модемы сохранены. Перезапустите сервис для применения списка.';
      await refresh();
      return true;
    } finally {saving = null; save.disabled = false;}
  };
  form.addEventListener('submit', async event => {
    event.preventDefault();
    try {await form.saveChanges();}
    catch (error) {notice.textContent = error.message;}
  });
})();
