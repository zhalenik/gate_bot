(() => {
  const get = id => document.getElementById(id);
  const form = get('apiTokenForm'), output = get('apiTokenValue'), result = get('apiTokenResult'), status = get('apiTokenStatus');
  const headers = {'X-CSRF-Token': document.querySelector('meta[name=csrf-token]').content, 'Content-Type': 'application/json'};
  for (const item of document.querySelectorAll('#apiTokens time')) item.textContent = new Date(item.dateTime).toLocaleString('ru-RU');
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const scopes = Array.from(form.querySelectorAll('input[name=scope]:checked'));
    if (!scopes.length) { status.textContent = 'Выберите права токена.'; return; }
    const button = event.submitter; button.disabled = true;
    try {
      const name = form.elements.name.value.trim(), limit = Number(form.elements.rate.value);
      const response = await fetch('/admin/api/service-tokens', {method: 'POST', headers,
        body: JSON.stringify({name, scopes: scopes.map(item => item.value), rate_limit: limit})});
      if (!response.ok) throw Error('Не удалось создать токен. Проверьте название, права и лимит.');
      const data = await response.json(), row = document.createElement('tr');
      for (const value of [name, scopes.map(item => item.parentElement.textContent.trim()).join(', '), limit, 0, '—']) {
        const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
      }
      const revoke = document.createElement('button'); revoke.type = 'button'; revoke.className = 'btn-danger';
      revoke.dataset.revoke = String(data.id); revoke.textContent = 'Отозвать'; row.insertCell().append(revoke); get('apiTokens').prepend(row);
      output.value = data.token; result.hidden = false; form.hidden = true; output.focus(); output.select();
      status.textContent = 'Токен показывается один раз.';
    } catch (error) { status.textContent = error.message; }
    finally { button.disabled = false; }
  });
  get('apiTokenCopy').onclick = async () => {
    try { await navigator.clipboard.writeText(output.value); status.textContent = 'Токен скопирован.'; }
    catch { output.focus(); output.select(); status.textContent = 'Скопируйте выделенный токен.'; }
  };
  function clearSecret() { output.value = ''; result.hidden = true; form.hidden = false; form.reset(); status.textContent = ''; }
  get('apiTokenDone').onclick = clearSecret;
  window.addEventListener('pagehide', clearSecret);
  get('apiTokens').addEventListener('click', async event => {
    const button = event.target.closest('[data-revoke]');
    if (!button || !window.confirm('Отозвать этот токен? Интеграция потеряет доступ.')) return;
    button.disabled = true;
    try {
      const response = await fetch('/admin/api/service-tokens/' + button.dataset.revoke, {method: 'DELETE', headers});
      if (!response.ok) throw Error('Не удалось отозвать токен.');
      button.closest('tr').remove(); clearSecret(); status.textContent = 'Токен отозван.';
    } catch (error) { status.textContent = error.message; button.disabled = false; }
  });
})();
