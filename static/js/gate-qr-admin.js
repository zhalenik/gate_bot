(() => {
  'use strict';
  const make = (tag, text, className) => { const node = document.createElement(tag); if (text) node.textContent = text; if (className) node.className = className; return node; };
  function confirmAction(title, message, label) {
    return new Promise(resolve => {
      const dialog = make('dialog', '', 'qr-dialog'), actions = make('div', '', 'qr-dialog-actions');
      const cancel = make('button', 'Отмена'), yes = make('button', label, 'danger');
      cancel.type = yes.type = 'button';
      cancel.onclick = () => dialog.close(); yes.onclick = () => dialog.close('confirmed');
      actions.append(cancel, yes); dialog.append(make('h2', title), make('p', message), actions);
      dialog.addEventListener('close', () => { const confirmed = dialog.returnValue === 'confirmed'; dialog.remove(); resolve(confirmed); });
      document.body.append(dialog); dialog.showModal(); cancel.focus();
    });
  }
  async function api(url, action) {
    const response = await fetch(url, {credentials: 'same-origin', ...(action ? {method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content}, body: JSON.stringify({action})} : {})});
    let result;
    try { result = await response.json(); } catch { throw new Error('Обновите страницу и войдите в систему.'); }
    if (!response.ok) throw new Error(result.message || 'Не удалось загрузить QR-код');
    return result;
  }
  async function openDialog(branchId, gateId) {
    const endpoint = '/admin/api/branches/' + encodeURIComponent(branchId) + '/gates/' + encodeURIComponent(gateId) + '/qr';
    const dialog = make('dialog', '', 'qr-dialog');
    const heading = make('div', '', 'qr-dialog-heading'), title = make('h2', 'QR-код ворот'), close = make('button', '×', 'icon-button');
    close.type = 'button'; close.setAttribute('aria-label', 'Закрыть'); close.addEventListener('click', () => dialog.close());
    heading.append(title, close);
    const description = make('p', 'Разместите код у ворот. Клиент введёт свой телефон и подтвердит его по SMS. Открытие доступно только с действующим пропуском этого филиала.');
    const picture = make('img'); picture.alt = 'QR-код для доступа к этим воротам'; picture.hidden = true;
    const link = make('input'); link.readOnly = true; link.setAttribute('aria-label', 'Постоянная ссылка ворот'); link.hidden = true;
    const actions = make('div', '', 'qr-dialog-actions'), status = make('p'); status.dataset.role = 'status'; status.setAttribute('role', 'status');
    dialog.append(heading, description, picture, link, actions, status);
    document.body.append(dialog); dialog.showModal();
    dialog.addEventListener('close', () => dialog.remove());
    function action(label, run, danger = false) {
      const button = make('button', label, danger ? 'danger' : ''); button.type = 'button';
      button.addEventListener('click', async () => {
        Array.from(actions.querySelectorAll('button')).forEach(item => { item.disabled = true; });
        try { await run(); } catch (error) { status.textContent = error.message; }
        finally { Array.from(actions.querySelectorAll('button')).forEach(item => { item.disabled = false; }); }
      }); actions.append(button);
    }
    function render(data) {
      title.textContent = data.gate_label || 'QR-код ворот'; actions.replaceChildren();
      picture.hidden = link.hidden = !data.enabled;
      if (data.enabled) {
        picture.src = endpoint + '.svg?v=' + Date.now(); link.value = data.url;
        for (const type of ['svg', 'png']) {
          const download = make('a', 'Скачать ' + type.toUpperCase()); download.href = endpoint + '.' + type + '?download=1'; actions.append(download);
        }
        action('Скопировать ссылку', async () => {
          if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(data.url); status.textContent = 'Ссылка скопирована.'; }
          else { link.focus(); link.select(); status.textContent = 'Ссылка выделена — скопируйте её.'; }
        });
        action('Заменить QR', async () => {
          if (await confirmAction('Заменить QR-код?', 'Распечатанные ранее коды перестанут работать. Понадобится разместить новый код.', 'Заменить')) {
            render(await api(endpoint, 'rotate')); status.textContent = 'Новый код создан. Старый больше не действует.';
          }
        });
        action('Отключить', async () => {
          if (await confirmAction('Отключить QR-код?', 'Вход через этот код станет недоступен. Действующие персональные пропуска сохранятся.', 'Отключить')) {
            render(await api(endpoint, 'revoke')); status.textContent = 'QR-код отключён.';
          }
        }, true);
      } else {
        status.textContent = 'QR-код пока не создан или отключён.';
        action('Создать QR-код', async () => { render(await api(endpoint, 'create')); status.textContent = 'Код готов для печати.'; });
      }
    }
    try { render(await api(endpoint)); } catch (error) { status.textContent = error.message; }
  }
  function decorate(branchId) {
    document.querySelectorAll('[data-field="type"]').forEach(select => {
      const row = select.closest('tr'); if (!row) return;
      row.querySelector('.qr-admin-control')?.remove();
      if (select.value !== 'gate' || !row.dataset.stableId || !branchId) return;
      const control = make('div', '', 'qr-admin-control'), button = make('button', 'QR-код'); button.type = 'button';
      const stableId = row.dataset.stableId;
      button.addEventListener('click', () => openDialog(branchId, stableId)); control.append(button); row.lastElementChild.append(control);
      select.addEventListener('change', () => control.remove(), {once: true});
    });
  }
  document.addEventListener('gatebot:branch-settings-loaded', event => decorate(event.detail.branchId));
  decorate(document.getElementById('settingsBranchId')?.value);
})();
