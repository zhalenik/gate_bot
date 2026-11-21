'use strict';
(() => {
  const boot = JSON.parse(document.getElementById('identityBootstrap').textContent);
  if (!['integrations','messenger_only'].includes(boot.kind)) return;
  const branch = document.getElementById('integrationBranch');
  if (!branch) return;
  const el = (tag, text = '', cls = '') => { const n = document.createElement(tag); n.textContent = text; n.className = cls; return n; };
  const panel = el('section', '', 'panel'); panel.id = 'branchMessengerSettings';
  if(boot.kind==='integrations')panel.append(el('h2', 'Сообщения клиентам'));
  const form = el('form', '', 'admin-form'), label = el('label', 'Способ отправки', 'form-group'), select = el('select');
  select.name = 'messenger_provider';
  const providers = {auto:'Автоматически',yclients:'Сервис рассылки в YCLIENTS',chatpush:'ChatPush напрямую',sms:'СМС филиала'};
  label.append(select); form.append(label);
  const description = el('p', '', 'page-description'), status = el('p', '', 'form-hint'); status.setAttribute('role', 'status');
  const save = el('button', 'Сохранить способ отправки', 'btn-primary'); save.type = 'submit';
  form.append(description, save); panel.append(form, status); document.getElementById('identityContent').append(panel);
  let revision = 0, saving = false, dirty = false;
  async function api(data) {
    const response = await fetch('/admin/api/messenger-settings' + (data ? '' : '?branch_id=' + encodeURIComponent(branch.value)), {
      credentials: 'same-origin', method: data ? 'POST' : 'GET',
      headers: data ? {'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name=csrf-token]').content} : {},
      body: data ? JSON.stringify(data) : undefined
    });
    const result = await response.json(); if (!response.ok) throw Error(result.message || 'Не удалось сохранить настройки'); return result.messaging;
  }
  function explain() {
    if(boot.kind==='messenger_only'){
      description.textContent='Сообщения отправляются по телефону из квоты СМС филиала.';
      return;
    }
    description.textContent = {
      auto:'Для клиента с ID YCLIENTS используется рассылка YCLIENTS. Следующий доступный способ — ChatPush, затем СМС филиала.',
      chatpush:'Сообщение отправляется через ChatPush по номеру телефона.',
      sms:'Сообщение отправляется по номеру телефона и расходует квоту СМС филиала.',
      yclients:'Рассылка YCLIENTS доступна клиентам с ID в этом филиале.'
    }[select.value] || '';
  }
  function paint(value, preserveSelection = false) {
    const previous=select.value;select.replaceChildren();
    for(const [key,title] of Object.entries(providers)){
      if(key!=='auto'&&!value.available[key])continue;
      const option=el('option',title);option.value=key;select.append(option);
    }
    const selected=preserveSelection&&dirty?previous:value.selected;
    select.value=(selected==='auto'||value.available[selected])?selected:'auto';
    explain();
  }
  async function refresh() {
    if (saving) return; const version = ++revision; save.disabled = true; select.disabled = true;
    try { const value = await api(); if (version !== revision) return; paint(value, true); status.textContent = value.configured ? 'Сервис подключён' : 'Для отправки подключите выбранный сервис'; }
    catch (error) { if (version === revision) status.textContent = error.message; }
    finally { if (version === revision) { save.disabled = false; select.disabled = false; } }
  }
  select.addEventListener('change', () => { dirty = true; explain(); });
  branch.addEventListener('change', () => { revision++; dirty = false; refresh(); });
  form.addEventListener('submit', async event => {
    event.preventDefault(); const version = ++revision, selectedBranch = branch.value; saving = true; save.disabled = true; select.disabled = true;
    try { const value = await api({branch_id: selectedBranch, provider: select.value}); if (version === revision && selectedBranch === branch.value) { dirty = false; paint(value); status.textContent = 'Способ отправки сохранён'; } }
    catch (error) { if (version === revision) status.textContent = error.message; }
    finally { saving = false; if (selectedBranch !== branch.value) refresh(); else { save.disabled = false; select.disabled = false; } }
  });
  document.addEventListener('gatebot:integrations-updated', refresh);
  addEventListener('focus', refresh); refresh();
})();
