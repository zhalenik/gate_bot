'use strict';
(() => {
  const bootstrap = document.getElementById('identityBootstrap');
  if (!bootstrap || JSON.parse(bootstrap.textContent).kind !== 'integrations') return;
  const branch = document.getElementById('integrationBranch');
  if (!branch) return;
  const make = (tag, text = '', className = '') => {
    const node = document.createElement(tag); node.textContent = text; node.className = className; return node;
  };
  const panel = make('section', '', 'panel yclients-sync-panel'); panel.id = 'yclientsClientSyncPanel';
  panel.append(make('h2', 'Связь клиентов с YCLIENTS'), make('p', 'Обновите ID клиентов по номерам телефонов в клиентской базе этого филиала. При неоднозначном совпадении ID остаётся прежним.', 'page-description'));
  const actions = make('div', '', 'yclients-sync-actions'), start = make('button', 'Обновить ID всех клиентов', 'btn-primary');
  start.type = 'button'; start.id = 'yclientsClientSyncStart'; actions.append(start); panel.append(actions);
  const progress = make('progress'); progress.id = 'yclientsClientSyncProgress'; progress.max = 1; progress.value = 0; progress.hidden = true;
  progress.setAttribute('aria-label', 'Проверка ID клиентов');
  const status = make('p', '', 'form-hint'), summary = make('p', '', 'form-hint');
  status.id = 'yclientsClientSyncStatus'; status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  summary.id = 'yclientsClientSyncSummary'; panel.append(progress, status, summary);
  document.getElementById('identityContent').append(panel);
  let generation = 0, loading = false, starting = false, state = null, timer = null;
  const active = () => ['queued', 'running'].includes(state?.state);
  const number = value => Math.max(0, Number(value) || 0);
  function controls() { start.disabled = loading || starting || active() || state?.can_start === false; }
  function clearTimer() { if (timer !== null) clearTimeout(timer); timer = null; }
  function schedule() {
    clearTimer();
    if (active() && !document.hidden) timer = setTimeout(refresh, 1500);
  }
  function paint(value) {
    state = value || {state: 'idle'};
    const total = number(state.total), processed = Math.min(number(state.processed), total);
    progress.hidden = !active() && !total; progress.max = Math.max(total, 1); progress.value = processed;
    const labels = {idle: 'Обновление пока не запускалось.', queued: 'Обновление ожидает выполнения.', running: 'Проверяем номера в YCLIENTS…', complete: 'Обновление завершено.', failed: 'Обновление не завершено.'};
    status.textContent = state.message || labels[state.state] || 'Статус обновлён.';
    status.className = state.state === 'failed' ? 'alert alert-error' : 'form-hint';
    const stats = [];
    if (total || active()) stats.push(`Проверено ${processed} из ${total}`);
    for (const [key, title] of [['updated', 'Обновлено'], ['unchanged', 'Без изменений'], ['not_found', 'Не найдены'], ['ambiguous', 'Неоднозначные совпадения'], ['conflicts', 'Конфликты'], ['errors', 'Ошибки']]) {
      if (number(state[key])) stats.push(title + ': ' + number(state[key]));
    }
    summary.textContent = stats.join(' · '); summary.hidden = !stats.length;
    controls(); schedule();
  }
  async function refresh() {
    if (starting || !branch.value) return;
    clearTimer(); const current = ++generation, branchId = branch.value; loading = true; controls();
    try {
      const result = await api('/admin/api/yclients/client-ids?branch_id=' + encodeURIComponent(branchId));
      if (current !== generation || branchId !== branch.value) return;
      paint(result.sync);
    } catch (error) {
      if (current === generation) { status.textContent = error.message; status.className = 'alert alert-error'; }
    } finally {
      if (current === generation) { loading = false; controls(); schedule(); }
    }
  }
  start.addEventListener('click', async () => {
    if (start.disabled) return;
    clearTimer(); const current = ++generation, branchId = branch.value; starting = true; controls();
    status.textContent = 'Запускаем проверку…'; status.className = 'form-hint';
    try {
      const result = await post('/admin/api/yclients/client-ids', {branch_id: branchId});
      if (current !== generation || branchId !== branch.value) return;
      paint(result.sync);
    } catch (error) {
      if (current === generation) { status.textContent = error.message; status.className = 'alert alert-error'; }
    } finally {
      starting = false;
      if (current === generation) { controls(); schedule(); }
      else refresh();
    }
  });
  branch.addEventListener('change', () => {
    generation++; clearTimer(); state = null; loading = false; progress.hidden = true; summary.textContent = '';
    status.textContent = 'Загружаем состояние…'; controls(); refresh();
  });
  document.addEventListener('gatebot:integrations-updated', () => { if (!starting) refresh(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) clearTimer(); else refresh(); });
  window.addEventListener('focus', refresh);
  window.addEventListener('pagehide', clearTimer);
  controls(); refresh();
})();
