'use strict';
(() => {
  const bootstrap = document.getElementById('identityBootstrap');
  if (!bootstrap || JSON.parse(bootstrap.textContent).kind !== 'integrations') return;
  const branch = document.getElementById('integrationBranch');
  if (!branch) return;
  const make = (tag, text = '', className = '') => {
    const node = document.createElement(tag); node.textContent = text; node.className = className; return node;
  };
  const panel = make('section', '', 'panel integration-test-panel'); panel.id = 'integrationTestPanel';
  panel.append(make('h2', 'Тестовое сообщение'), make('p', 'Проверьте подключение на одном получателе. Сообщение будет отправлено через выбранный сервис; настройки рассылки клиента останутся прежними.', 'page-description'));
  const form = make('form', '', 'admin-form integration-test-form');
  const field = (title, tag, id) => {
    const label = make('label', title, 'form-group'), node = make(tag); node.id = id; label.append(node); form.append(label); return node;
  };
  const option = (value, text) => { const node = make('option', text); node.value = value; return node; };
  const provider = field('Сервис тестовой отправки', 'select', 'integrationTestProvider');
  provider.append(option('chatpush', 'Сервис рассылки по телефону'), option('yclients', 'YCLIENTS'));
  const ready = make('p', '', 'form-hint'); ready.id = 'integrationTestConnection'; ready.setAttribute('role', 'status'); form.append(ready);
  const mode = field('Как выбрать получателя', 'select', 'integrationTestRecipientMode');
  mode.append(option('phone', 'Ввести номер телефона'), option('client', 'Выбрать клиента филиала'));
  const phone = field('Номер телефона получателя', 'input', 'integrationTestPhone');
  phone.type = 'tel'; phone.autocomplete = 'tel'; phone.maxLength = 64; phone.placeholder = '+7…';
  const search = field('Поиск клиента', 'input', 'integrationTestClientSearch'); search.type = 'search'; search.autocomplete = 'off';
  search.placeholder = 'Имя или номер телефона';
  const client = field('Клиент филиала', 'select', 'integrationTestClient');
  const recipientHint = make('p', '', 'form-hint'); form.append(recipientHint);
  const text = field('Текст тестового сообщения', 'textarea', 'integrationTestText');
  const defaultText = 'Тестовое сообщение GateBot. Подключение работает.';
  text.rows = 4; text.required = true; text.value = defaultText;
  const counter = make('span', '', 'form-hint'); counter.id = 'integrationTestCounter'; text.parentElement.append(counter);
  const actions = make('div', '', 'integration-test-actions'), send = make('button', 'Отправить тестовое сообщение', 'btn-primary');
  send.type = 'submit'; send.id = 'integrationTestSend'; actions.append(send); form.append(actions);
  const status = make('p', '', 'form-hint'); status.id = 'integrationTestStatus'; status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite'); form.append(status);
  panel.append(form); document.getElementById('identityContent').append(panel);
  let state = null, generation = 0, loading = false, sending = false, attempt = null, uncertain = false;
  const manual = () => provider.value === 'chatpush' && mode.value === 'phone';
  const limit = () => state?.max_length || (provider.value === 'chatpush' ? 3500 : 16000);
  const uuid = () => crypto.randomUUID ? crypto.randomUUID() : ([1e7] + '-1e3-4e3-8e3-1e11').replace(/[018]/g, digit => (digit ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> digit / 4).toString(16));
  function tell(message, error = false) { status.textContent = message; status.className = error ? 'alert alert-error' : 'form-hint'; }
  function changed() { attempt = null; uncertain = false; tell(''); update(); }
  function update() {
    const isManual = manual(), hasClients = Boolean(state?.clients?.length);
    mode.parentElement.hidden = provider.value !== 'chatpush';
    phone.parentElement.hidden = !isManual; phone.disabled = !isManual || sending; phone.required = isManual;
    search.parentElement.hidden = isManual; client.parentElement.hidden = isManual;
    search.disabled = sending || loading || !hasClients; client.disabled = isManual || sending || loading || !hasClients; client.required = !isManual;
    provider.disabled = mode.disabled = text.disabled = sending;
    text.maxLength = limit(); counter.textContent = `${text.value.length} / ${limit()}`;
    send.disabled = sending || loading || !state?.ready || !text.value.trim() || text.value.length > limit() || (isManual ? !phone.value.trim() : !client.value);
    send.textContent = sending ? 'Отправляем…' : uncertain ? 'Проверить результат' : 'Отправить тестовое сообщение';
    recipientHint.textContent = isManual ? 'Можно указать номер, которого ещё нет в клиентской базе.'
      : !loading && !hasClients ? (provider.value === 'yclients' ? 'В филиале пока нет клиентов с ID YCLIENTS.' : 'В филиале пока нет клиентов с номером телефона.')
      : provider.value === 'yclients' ? 'В списке только клиенты этого филиала с ID YCLIENTS.' : '';
  }
  function paintClients() {
    const selected = client.value, needle = search.value.trim().toLocaleLowerCase();
    client.replaceChildren(option('', 'Выберите клиента'));
    for (const item of state?.clients || []) {
      const label = [item.name || 'Без имени', item.phone, provider.value === 'yclients' && item.yclients_id ? `YC ${item.yclients_id}` : ''].filter(Boolean).join(' · ');
      if (!needle || label.toLocaleLowerCase().includes(needle) || item.token === selected) client.append(option(item.token, label));
    }
    client.value = selected;
    if (!client.value) client.value = '';
  }
  async function api(path, body) {
    const response = await fetch(path, {method: body ? 'POST' : 'GET', credentials: 'same-origin',
      headers: body ? {'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name=csrf-token]').content} : {},
      body: body ? JSON.stringify(body) : undefined});
    let result;
    try { result = await response.json(); }
    catch (_) { throw Error('Не удалось получить ответ сервера. Повторное нажатие проверит тот же запрос без новой отправки.'); }
    if (!response.ok) { const error = Error(result.message || 'Не удалось выполнить запрос'); error.known = response.status < 500; error.status = response.status; throw error; }
    return result;
  }
  async function refresh() {
    const current = ++generation, branchId = branch.value, service = provider.value;
    loading = true; ready.textContent = 'Проверяем подключение…'; update();
    try {
      const result = await api('/admin/api/integration-test?branch_id=' + encodeURIComponent(branchId) + '&provider=' + encodeURIComponent(service));
      if (current !== generation || branchId !== branch.value || service !== provider.value) return;
      const previousClient = client.value; state = result.test; paintClients();
      if (previousClient && !client.value) changed();
      ready.textContent = state.message || (state.ready ? 'Можно отправить тестовое сообщение' : 'Сначала подключите выбранный сервис');
    } catch (error) { if (current === generation) { state = null; ready.textContent = error.message; paintClients(); } }
    finally { if (current === generation) { loading = false; update(); } }
  }
  provider.addEventListener('change', () => { state = null; client.value = ''; search.value = ''; changed(); refresh(); });
  branch.addEventListener('change', () => { state = null; phone.value = ''; client.value = ''; search.value = ''; text.value = defaultText; changed(); refresh(); });
  mode.addEventListener('change', changed); phone.addEventListener('input', changed); text.addEventListener('input', changed); client.addEventListener('change', changed);
  search.addEventListener('input', () => { paintClients(); update(); });
  document.addEventListener('gatebot:integrations-updated', () => { if (!sending) refresh(); });
  window.addEventListener('focus', () => { if (!sending) refresh(); });
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (sending || send.disabled || !form.reportValidity()) return;
    const branchId = branch.value, service = provider.value, current = generation;
    const payload = {branch_id: branchId, provider: service, text: text.value,
      ...(manual() ? {phone: window.GateBotPhone?.normalize(phone.value) || phone.value} : {client_token: client.value})};
    const fingerprint = JSON.stringify(payload);
    if (!attempt || attempt.fingerprint !== fingerprint) attempt = {fingerprint, id: uuid()};
    const currentAttempt = attempt; sending = true; tell(''); update();
    try {
      const result = await api('/admin/api/integration-test', {...payload, request_id: currentAttempt.id});
      if (current !== generation || branchId !== branch.value || service !== provider.value) return;
      const delivery = result.delivery || {}, accepted = ['accepted', 'sent', 'success'].includes(delivery.status);
      uncertain = ['unknown', 'sending', 'pending'].includes(delivery.status) || !delivery.status;
      if (!uncertain) attempt = null;
      tell(delivery.message || (accepted ? 'Сервис принял сообщение. Получение клиентом пока не подтверждено.'
        : uncertain ? 'Результат не подтверждён. Повторное нажатие проверит тот же запрос без новой отправки.' : 'Сервис не принял сообщение. Проверьте подключение и получателя.'), !accepted);
    } catch (error) {
      if (current !== generation || branchId !== branch.value || service !== provider.value) return;
      uncertain = !error.known || error.status === 409;
      if (!uncertain) attempt = null;
      tell(error.known ? error.message : 'Не удалось подтвердить результат. Повторное нажатие проверит тот же запрос без новой отправки.', true);
    } finally { sending = false; update(); }
  });
  update(); refresh();
})();
