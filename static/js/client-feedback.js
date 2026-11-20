'use strict';
(() => {
  const footer = document.querySelector('[data-feedback-token]');
  if (!footer) return;
  const dialog = document.querySelector('.client-feedback-dialog');
  const form = dialog.querySelector('form');
  const status = form.querySelector('[role=status]');
  let sending = false;
  async function post(action, body) {
    const response = await fetch('/api/client/feedback/' + action, {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name=csrf-token]').content},
      body: JSON.stringify({...body, token: footer.dataset.feedbackToken})
    });
    const result = await response.json();
    if (!response.ok) throw Error(result.message || 'Не удалось отправить отзыв');
    return result;
  }
  function show() {
    if (dialog.open) return;
    status.textContent = '';
    form.querySelector('button[type=submit], .btn-primary').hidden = false;
    dialog.showModal();
  }
  footer.querySelector('button').onclick = show;
  form.querySelector('[data-feedback-close]').onclick = () => dialog.close();
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (sending) return;
    const values = new FormData(form);
    sending = true;
    try {
      const result = await post('send', {stars: Number(values.get('stars')) || null, message: values.get('message')});
      status.textContent = result.message; form.reset(); form.querySelector('.btn-primary').hidden = true;
    } catch (error) { status.textContent = error.message; }
    finally { sending = false; }
  });
  document.addEventListener('gatebot:gate-command', async event => {
    try {
      if ((await post('prompt', {request_id: event.detail})).show) show();
    } catch { /* The gate command has already completed independently. */ }
  });
})();
