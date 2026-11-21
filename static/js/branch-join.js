(() => {
  const element = id => document.getElementById(id);
  const phoneForm = element('joinPhone'), codeForm = element('joinCode'), profile = element('joinProfile');
  const resend = element('resendJoin'), retry = element('retryJoin'), status = element('joinStatus');
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  let challenge, timer, resendAt = 0;
  async function post(action, payload) {
    const response = await fetch(location.pathname + '/' + action, {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: JSON.stringify(payload)});
    const result = await response.json();
    if (!response.ok) {
      if (result.retry_after) countdown(result.retry_after);
      throw new Error(result.message || 'Не удалось выполнить запрос.');
    }
    return result;
  }
  function countdown(seconds) {
    clearInterval(timer);
    resendAt = Date.now() + seconds * 1000;
    const update = () => {
      const left = Math.max(0, Math.ceil((resendAt - Date.now()) / 1000));
      resend.disabled = left > 0;
      resend.textContent = left ? 'Повторная отправка через ' + left + ' с' : 'Отправить повторно';
      if (!left) clearInterval(timer);
    };
    update(); timer = setInterval(update, 1000);
  }
  async function sendCode() {
    const documents=window.registrationChoices();if(!documents)return;
    const result = await post('code', {phone: element('phone').value,documents});
    countdown(result.retry_after);
    if (!result.ready) throw new Error('Не удалось отправить код. Повторите позже.');
    codeForm.querySelector('label').textContent = result.channel === 'chatpush' ? 'Код из мессенджера' : 'Код из SMS';
    challenge = result.challenge_id; phoneForm.hidden = true; codeForm.hidden = false; element('code').focus();
  }
  async function loadProfile() {
    retry.hidden = false;
    const values = await post('profile', {});
    element('name').value = values.name; element('email').value = values.email;
    if (values.existing) {
      element('name').readOnly = element('email').readOnly = true;
      profile.querySelector('button').textContent = 'Перейти к пропуску';
    }
    retry.hidden = true; profile.hidden = false; element('name').focus();
  }
  function on(control, eventName, run) {
    control.addEventListener(eventName, async event => {
      event.preventDefault();
      const button = event.submitter || control;
      button.disabled = true; status.textContent = '';
      try { await run(); } catch (error) { status.textContent = error.message; }
      finally { button.disabled = button === resend && resendAt > Date.now(); }
    });
  }
  on(phoneForm, 'submit', sendCode); on(resend, 'click', sendCode);
  on(codeForm, 'submit', async () => {
    await post('verify', {challenge_id: challenge, code: element('code').value,documents:window.registrationChoices()});
    codeForm.hidden = true; await loadProfile();
  });
  on(retry, 'click', loadProfile);
  on(profile, 'submit', async () => {
    const result = await post('finish', {name: element('name').value, email: element('email').value});
    location.replace(result.url);
  });
})();
