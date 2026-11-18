'use strict';
(() => {
  const boot = JSON.parse(document.getElementById('identityBootstrap').textContent);
  if (boot.kind !== 'integrations') return;
  const branch = document.getElementById('integrationBranch');
  if (!branch) return;
  const el = (tag, text = '', cls = '') => { const n = document.createElement(tag); n.textContent = text; n.className = cls; return n; };
  const panel = el('section', '', 'panel'); panel.id = 'branchMessengerSettings';
  panel.append(el('h2', 'Сообщения клиентам'), el('p', 'Канал «Мессенджеры» в настройках клиента использует выбранный здесь сервис.', 'page-description'));
  const form = el('form', '', 'admin-form'), label = el('label', 'Способ отправки', 'form-group'), select = el('select');
  select.name = 'messenger_provider';
  for (const [value, text] of [['auto', 'Автоматически'], ['yclients', 'Сервис рассылки в YCLIENTS'], ['chatpush', 'ChatPush напрямую']]) {
    const option = el('option', text); option.value = value; select.append(option);
  }
  label.append(select); form.append(label);
  const description = el('p', '', 'page-description'), status = el('p', '', 'form-hint'); status.setAttribute('role', 'status');
  const save = el('button', 'Сохранить способ отправки', 'btn-primary'); save.type = 'submit';
  form.append(description, save); panel.append(form, status); document.getElementById('identityContent').append(panel);
  let revision = 0, state = null, saving = false, dirty = false;
  async function api(data) {
    const response = await fetch('/admin/api/messenger-settings' + (data ? '' : '?branch_id=' + encodeURIComponent(branch.value)), {
      credentials: 'same-origin', method: data ? 'POST' : 'GET',
      headers: data ? {'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name=csrf-token]').content} : {},
      body: data ? JSON.stringify(data) : undefined
    });
    const result = await response.json(); if (!response.ok) throw Error(result.message || 'Не удалось сохранить настройки'); return result.messaging;
  }
  function explain() {
    const provider = select.value === 'auto' ? (state?.available.chatpush ? 'chatpush' : 'yclients') : select.value;
    description.textContent = provider === 'chatpush'
      ? 'ChatPush отправляет сообщения по номеру телефона, в том числе гостям без карточки YCLIENTS.'
      : 'YCLIENTS отправляет через сервис рассылки филиала. Нужен ID клиента в YCLIENTS; гостям без него сообщение не отправится. Конечный канал, включая возможную SMS, зависит от настроек провайдера в YCLIENTS.';
    if (select.value === 'auto') description.textContent += ' Автоматически: подключённый ChatPush, иначе YCLIENTS.';
  }
  function paint(value, preserveSelection = false) { const selected = select.value; state = value; select.value = preserveSelection && dirty ? selected : value.selected; select.querySelector('option[value=chatpush]').disabled = !value.available.chatpush; explain(); }
  async function refresh() {
    if (saving) return; const version = ++revision; save.disabled = true; select.disabled = true;
    try { const value = await api(); if (version !== revision) return; paint(value, true); status.textContent = value.configured ? 'Сервис подключён' : 'Для отправки подключите выбранный сервис'; }
    catch (error) { if (version === revision) status.textContent = error.message; }
    finally { if (version === revision) { save.disabled = false; select.disabled = false; } }
  }
  select.addEventListener('change', () => { dirty = true; explain(); });
  branch.addEventListener('change', () => { revision++; state = null; dirty = false; refresh(); });
  form.addEventListener('submit', async event => {
    event.preventDefault(); const version = ++revision, selectedBranch = branch.value; saving = true; save.disabled = true; select.disabled = true;
    try { const value = await api({branch_id: selectedBranch, provider: select.value}); if (version === revision && selectedBranch === branch.value) { dirty = false; paint(value); status.textContent = 'Способ отправки сохранён'; } }
    catch (error) { if (version === revision) status.textContent = error.message; }
    finally { saving = false; if (selectedBranch !== branch.value) refresh(); else { save.disabled = false; select.disabled = false; } }
  });
  document.addEventListener('gatebot:integrations-updated', refresh);
  addEventListener('focus', refresh); refresh();
})();
