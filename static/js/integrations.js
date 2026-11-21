'use strict';
(() => {
  const boot = JSON.parse(document.getElementById('identityBootstrap').textContent);
  if (boot.kind !== 'integrations') return;
  const root = document.getElementById('identityContent');
  const notice = document.getElementById('identityNotice');
  const element = (tag, text = '', className = '') => {
    const node = document.createElement(tag); node.textContent = text; node.className = className; return node;
  };
  const tell = (text, error = false) => { notice.hidden = false; notice.className = 'alert ' + (error ? 'alert-error' : 'alert-info'); notice.textContent = text; };
  if (!boot.branches.length) { root.append(element('p', 'Подключения доступны филиалам, работающим с YCLIENTS.')); return; }
  document.getElementById('identityDescription').textContent = 'Подключите записи и уведомления. Сохранённые токены остаются на сервере.';
  const selector = element('select'); selector.id = 'integrationBranch';
  for (const branch of boot.branches) { const option = element('option', branch.name); option.value = branch.id; selector.append(option); }
  const branchLabel = element('label', 'Филиал', 'integration-branch'); branchLabel.hidden = !boot.is_super; branchLabel.append(selector); root.append(branchLabel);
  const grid = element('div', '', 'integration-grid'); root.append(grid);
  const cards = {}; let generation = 0, refreshVersion = 0;
  const labels = {branch: 'Доступ к филиалу', records: 'Чтение записей', contacts: 'Телефоны клиентов в записях', client_directory: 'Клиентская база · поиск ID и email', all_staff: 'Все сотрудники филиала', records_api: 'API записей'};
  const reasons = {ready: 'Подключение проверено', permissions_missing: 'Нужно настроить права пользователя', contacts_unconfirmed: 'Проверьте телефон клиента и права на телефоны в журнале', credentials_missing: 'Не хватает настроек подключения', authorization_failed: 'Войдите в YCLIENTS заново', access_denied: 'Нет доступа к филиалу', branch_unavailable: 'Филиал недоступен', rate_limited: 'Повторите проверку позже', transport_error: 'Нет связи с сервисом', invalid_response: 'Не удалось подтвердить доступ'};
  async function api(path, data) {
    const response = await fetch(path, {method: data === undefined ? 'GET' : 'POST', credentials: 'same-origin',
      headers: data === undefined ? {} : {'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name=csrf-token]').content},
      body: data === undefined ? undefined : JSON.stringify(data)});
    const value = await response.json();
    if (!response.ok) { const error = Error(value.message || 'Подключение не выполнено'); error.data = value; throw error; }
    return value;
  }
  function field(form, title, name, type = 'text', required = true) {
    const label = element('label', title, 'form-group'), input = element('input');
    input.name = name; input.type = type; input.required = required;
    input.autocomplete = type === 'password' ? 'new-password' : 'off'; label.append(input); form.append(label); return input;
  }
  function paint(kind, state = {}, diagnostic = null) {
    const card = cards[kind]; card.status.replaceChildren();
    if (kind === 'yclients') {
      const marketplace = state.mode === 'marketplace';
      card.heading.textContent = marketplace ? 'Приложение YCLIENTS Marketplace' : 'Учётная запись YCLIENTS';
      card.form.hidden = state.manual_allowed !== true;
      card.modeHint.textContent = marketplace ? 'Чтобы подключить филиал повторно, откройте карточку GateBot в маркетплейсе YCLIENTS.' : 'Прямое подключение от имени пользователя YCLIENTS.';
      card.disconnectInfo.hidden = !marketplace;
      if (card.account) {
        card.account.replaceChildren();
        const profile = state.account || {};
        for (const [key,title] of [['name','Пользователь'],['email','Email'],['phone','Телефон'],['login','Логин'],['id','ID пользователя']]) {
          if (profile[key]) card.account.append(element('p', title + ': ' + profile[key], 'page-description'));
        }
        if (!marketplace && state.configured && !Object.keys(profile).length) card.account.append(element('p', 'Для этого старого подключения данные пользователя не сохранены. Повторный вход заполнит их.', 'page-description'));
      }
    }
    if(card.configure)card.configure(state);
    const check = state.blocked || (!state.configured && diagnostic?.connected) ? null : diagnostic;
    const connection = state.blocked ? '○ Подключение остановлено' : state.configured ? (kind === 'chatpush' ? '✓ Токен сохранён' : '● Подключено') : '○ Не подключено';
    card.status.append(element('p', connection, 'page-description'));
    if (check) card.status.append(element('p', (check.connected ? '✓ ' : '! ') + (reasons[check.code] || 'Проверка не завершена'), 'page-description'));
    if (check?.checks?.length) {
      const list = element('ul', '', 'integration-checks');
      for (const item of check.checks) list.append(element('li', (item.state === 'success' ? '✓ ' : '! ') + labels[item.key]));
      card.status.append(list);
    }
  }
  async function refresh(yclientsCheck = null) {
    const branch = selector.value, version = ++refreshVersion;
    const result = await api('/admin/api/identity/integrations?branch_id=' + encodeURIComponent(branch));
    if (version !== refreshVersion || branch !== selector.value) return;
    for (const kind of Object.keys(cards)) paint(kind, result.integrations[kind], kind === 'yclients' ? yclientsCheck : null);
    document.dispatchEvent(new Event('gatebot:integrations-updated'));
  }
  function makeCard(kind, title, canCheck = true) {
    const panel = element('section', '', 'panel'), heading = element('h2', title), status = element('div'); status.setAttribute('role', 'status');
    const form = element('form', '', 'admin-form'), button = element('button', 'Подключить', 'btn-primary'); button.type = 'submit';
    const check = canCheck ? element('button', 'Проверить доступ', 'btn-action') : null;
    panel.append(heading, status, form); grid.append(panel); cards[kind] = {panel, heading, form, button, check, status};
    if (check) { check.type = 'button'; panel.append(check); }
    if (check) check.onclick = async () => {
      const branch = selector.value, version = generation; check.disabled = true;
      try { const result = await api('/admin/api/identity/integrations/' + kind + '/check', {branch_id: branch});
        if (version !== generation) return;
        await refresh(result);
        if (version === generation && !result.connected) tell(result.message || reasons[result.code] || 'Проверьте настройки подключения', true);
        else if (version === generation) notice.hidden = true;
      } catch (error) { if (version === generation) tell(error.message, true); }
      finally { if (version === generation) check.disabled = false; }
    };
    return cards[kind];
  }
  const yc = makeCard('yclients', 'Учётная запись YCLIENTS');yc.form.hidden = true;
  yc.panel.id='yclientsIntegration';
  yc.heading.after(document.getElementById('yclientsGuide').content.cloneNode(true));
  yc.modeHint = element('p', '', 'page-description');yc.account = element('div');yc.account.id = 'yclientsConnectedAccount';yc.panel.insertBefore(yc.modeHint,yc.form);yc.panel.insertBefore(yc.account,yc.form);
  yc.disconnectInfo = element('p', 'Отключение в маркетплейсе прекращает автоматическую выдачу пропусков по записям YCLIENTS. Подписка и профиль филиала в GateBot сохраняются. Для удаления аккаунта обратитесь в ', 'integration-disconnect-info');
  const support = element('a', 'поддержку GateBot'); support.href = '/admin/support';
  yc.disconnectInfo.append(support, '.'); yc.disconnectInfo.hidden = true; yc.panel.append(yc.disconnectInfo);
  yc.button.textContent = 'Войти в YCLIENTS';
  yc.form.append(element('p', 'Войдите под пользователем нужного филиала. GateBot проверит права и сохранит полученный токен. Пароль не сохраняется.', 'page-description'));
  const login = field(yc.form, 'Телефон или email в YCLIENTS', 'login'); login.autocomplete = 'username';
  const password = field(yc.form, 'Пароль YCLIENTS', 'password', 'password'); password.autocomplete = 'current-password';
  const code = field(yc.form, 'Код двухфакторной защиты', 'code', 'text', false); code.parentElement.hidden = true; code.inputMode = 'numeric';
  yc.form.append(yc.button);
  yc.form.onsubmit = async event => {
    event.preventDefault(); const branch = selector.value, version = generation; yc.button.disabled = true;
    try {
      const result = await api('/admin/api/identity/integrations/yclients/authorize', {branch_id: branch, login: login.value, password: password.value, code: code.value});
      if (version !== generation) return;
      code.parentElement.hidden = !result.requires_2fa; code.required = !!result.requires_2fa;
      if (result.requires_2fa) { tell('Введите код, который отправил YCLIENTS.'); code.focus(); }
      else { yc.form.reset(); tell('YCLIENTS подключён, необходимые права подтверждены.'); await refresh(result.check); }
    } catch (error) { if (version === generation) { password.value = ''; tell(error.message, true); await refresh(error.data?.check).catch(() => {}); } }
    finally { if (version === generation) yc.button.disabled = false; }
  };
  const cp = makeCard('chatpush', 'Сервис рассылки', false);
  cp.panel.id='deliveryService';
  cp.button.textContent='Сохранить';
  const providerLabel=element('label','Интеграция','form-group'), provider=element('select');
  provider.name='delivery_service';provider.setAttribute('aria-label','Интеграция');providerLabel.append(provider);cp.form.append(providerLabel);
  for(const [value,title] of [['disabled','Отключено'],['chatpush','ChatPush']]){
    const option=element('option',title);option.value=value;provider.append(option);
  }
  const token=field(cp.form,'Токен ChatPush','token','password',false);
  const tokenHint=element('p','','form-hint');tokenHint.id='deliveryTokenHint';
  token.setAttribute('aria-describedby',tokenHint.id);token.parentElement.after(tokenHint);
  let hasToken=false,edited=false;
  function updateFields(){
    const enabled=provider.value==='chatpush';
    token.parentElement.hidden=!enabled;tokenHint.hidden=!enabled;token.disabled=!enabled;token.required=enabled&&!hasToken;
    tokenHint.textContent=hasToken?'Токен сохранён. Чтобы заменить его, введите новый.':'Введите токен из кабинета ChatPush.';
  }
  cp.configure=state=>{
    hasToken=Boolean(state.has_token);
    if(!edited)provider.value=state.configured?'chatpush':'disabled';
    updateFields();
  };
  provider.onchange=()=>{edited=true;token.value='';updateFields();};
  token.oninput=()=>{edited=true;};cp.form.append(cp.button);
  cp.form.onsubmit=async event=>{
    event.preventDefault();const version=generation,selected=provider.value;cp.button.disabled=true;
    try{
      await api('/admin/api/identity/delivery-service',{branch_id:selector.value,provider:selected,
        credentials:selected==='chatpush' && token.value?{token:token.value}:{}});
      if(version!==generation)return;
      edited=false;token.value='';await refresh();tell(selected==='disabled'?'Сервис рассылки отключён.':'Сервис рассылки подключён.');
    }catch(error){if(version===generation)tell(error.message,true);}
    finally{if(version===generation)cp.button.disabled=false;}
  };
  selector.onchange = () => {
    generation++; edited=false;hasToken=false; notice.hidden = true;
    for (const [kind, card] of Object.entries(cards)) { card.form.reset(); card.button.disabled = false; if (card.check) card.check.disabled = false; paint(kind); }
    code.required = false; code.parentElement.hidden = true;
    refresh().catch(error => tell(error.message, true));
  };
  window.addEventListener('pagehide', () => { password.value = ''; code.value = ''; token.value = ''; });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      notice.hidden = true;
      refresh().catch(error => tell(error.message, true));
    }
  });
  refresh().catch(error => tell(error.message, true));
})();
