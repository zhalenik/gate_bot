(() => {
  const branch = document.getElementById('integrationBranch');
  if (!branch) return;
  const panel = document.createElement('section');
  panel.className = 'panel'; panel.id = 'qrDeliverySettings'; panel.hidden = true;
  panel.innerHTML = '<h2>Подтверждение телефона по QR</h2><form class="admin-form"><label class="form-group">Канал отправки кода<select name="channel"><option value="sms">SMS</option><option value="chatpush">Сервис рассылки</option></select></label><p class="page-description">Для QR филиала и ворот. Каждая часть SMS расходует квоту филиала.</p><button class="btn-primary">Сохранить</button></form><p role="status"></p>';
  document.getElementById('identityContent').append(panel);
  const form = panel.querySelector('form'), status = panel.querySelector('[role=status]');
  const url = () => '/admin/api/branches/' + encodeURIComponent(branch.value) + '/qr-delivery';
  let version = 0, current = null, saving = false;
  async function refresh() {
    const selected = branch.value, seq = ++version;
    if (selected !== current) { panel.hidden = true; status.textContent = ''; current = selected; }
    try {
      const response = await fetch(url()), state = await response.json();
      if (seq !== version || branch.value !== selected || saving) return;
      panel.hidden = !response.ok || !state.available;
      if (state.available) form.elements.channel.value = state.channel;
    } catch { if (seq === version) panel.hidden = true; }
  }
  form.addEventListener('submit', async event => {
    event.preventDefault(); saving = true;
    const button = event.submitter, selected = branch.value; button.disabled = true;
    try {
      const response = await fetch(url(), {method: 'POST', headers: {'Content-Type': 'application/json',
        'X-CSRF-Token': document.querySelector('meta[name=csrf-token]').content}, body: JSON.stringify({channel: form.elements.channel.value})});
      const data = await response.json();
      if (selected === branch.value) status.textContent = response.ok ? 'Канал сохранён.' : data.message || 'Не удалось сохранить канал.';
    } catch { if (selected === branch.value) status.textContent = 'Не удалось сохранить канал.'; }
    finally { saving = false; button.disabled = false; refresh(); }
  });
  branch.addEventListener('change', refresh);
  document.addEventListener('gatebot:integrations-updated', refresh);
  refresh();
})();
