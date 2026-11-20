'use strict';
(() => {
  const script = document.currentScript;
  const root = '/yclients/marketplace';
  const status = document.getElementById('frameStatus');

  async function openFrame() {
    const link = document.getElementById('openInTab');
    const ticket = location.hash.slice(1);
    if (/^[\w-]{43}$/.test(ticket)) {
      link.href = root + '/resume#' + ticket;
      link.hidden = false;
    }
    try {
      const response = await fetch(root + '/state?frame=1', {credentials: 'same-origin'});
      if (response.ok && Array.isArray((await response.json()).branches)) {
        location.replace(root + '?frame=1');
        return;
      }
    } catch (_) { /* The separate-tab link also works with third-party cookies blocked. */ }
    status.textContent = link.hidden ? 'Откройте подключение из карточки GateBot в YCLIENTS ещё раз.' : 'Чтобы продолжить регистрацию в этом браузере, откройте GateBot в отдельной вкладке.';
  }

  async function resume() {
    const ticket = location.hash.slice(1);
    const next = new URLSearchParams(location.search).get('next');
    history.replaceState(null, '', root + '/resume');
    try {
      const response = await fetch(root + '/resume', {
        method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content},
        body: JSON.stringify({ticket}),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.message || 'Начните подключение из YCLIENTS ещё раз.');
      location.replace(next === 'login' ? '/admin/login?next=marketplace' : root);
    } catch (error) {
      status.textContent = error.message || 'Не удалось открыть подключение. Попробуйте ещё раз из YCLIENTS.';
    }
  }

  if (script.dataset.mode === 'start') { openFrame(); return; }
  if (script.dataset.mode === 'resume') { resume(); return; }

  window.marketplaceFrame = {
    query: '?frame=1',
    loginUrl: root + '/resume?next=login#' + script.dataset.ticket,
    completed() {
      const container = document.querySelector('main .admin-content');
      const title = document.createElement('h1');
      title.textContent = 'Регистрация завершена';
      const link = document.createElement('a');
      link.href = '/admin/integrations';
      link.className = 'btn-primary';
      link.rel = 'noopener noreferrer';
      link.target = '_blank';
      link.textContent = 'Открыть кабинет GateBot';
      container.replaceChildren(title, link);
    },
  };
})();
