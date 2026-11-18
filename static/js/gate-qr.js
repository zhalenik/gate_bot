(() => {
  'use strict';
  const config = JSON.parse(document.getElementById('gateQrConfig').textContent);
  const $ = id => document.getElementById(id);
  const endpoint = '/gate/' + encodeURIComponent(config.raw);
  let challenge = null, resendUntil = 0, cooldownUntil = 0, expiresAt = 0, busy = false, currentState = 'phone_required';
  let generation = 0, refreshing = false;
  let nextHoursChange = 0;
  function status(message, error = false) {
    $('qrStatus').textContent = message || '';
    $('qrStatus').classList.toggle('error', error);
  }
  async function post(path, data = {}) {
    const response = await fetch(endpoint + path, {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content},
      body: JSON.stringify(data)});
    let result;
    try { result = await response.json(); } catch { throw new Error('Сессия истекла. Обновите страницу.'); }
    if (!response.ok) {
      const error = new Error(result.message || 'Не удалось выполнить действие');
      error.retryAfter = result.retry_after || 0;
      if(result.code==='outside_working_hours')render({state:'closed',message:result.message});
      throw error;
    }
    return result;
  }
  function tick() {
    if(nextHoursChange&&Date.now()>=nextHoursChange){nextHoursChange=0;render({state:'closed',message:'Извините, доступ возможен только в рабочие часы. Вы можете связаться с администратором.'});refresh();}
    if (currentState === 'active' && expiresAt && Date.now() >= expiresAt) {
      currentState = 'no_pass'; $('qrOpen').hidden = true;
      $('qrBadge').textContent = 'Нет пропуска'; $('qrBadge').classList.remove('active');
      $('qrHeading').textContent = 'Пропуск не действует';
      $('qrDescription').textContent = 'Свяжитесь с администратором филиала, чтобы получить доступ.';
    }
    const resend = Math.max(0, Math.ceil((resendUntil - Date.now()) / 1000));
    const cooldown = Math.max(0, Math.ceil((cooldownUntil - Date.now()) / 1000));
    $('qrSendCode').disabled = busy || currentState==='closed' || resend > 0;
    $('qrSendCode').textContent = resend ? 'Повторный код через ' + resend + ' с' : 'Продолжить →';
    $('qrResend').disabled = busy || currentState==='closed' || resend > 0;
    $('qrResend').textContent = resend ? 'Повторный код через ' + resend + ' с' : 'Отправить код повторно';
    $('qrVerify').disabled = busy || currentState==='closed';
    $('qrOpen').disabled = busy || currentState !== 'active' || cooldown > 0;
    $('qrCooldown').hidden = !cooldown || currentState !== 'active';
    $('qrCooldown').textContent = cooldown ? 'Следующее открытие через ' + cooldown + ' с' : '';
  }
  function render(data) {
    currentState = data.state;
    nextHoursChange=data.access_hours?.next_change_at?Date.parse(data.access_hours.next_change_at)+(data.server_time?Date.now()-data.server_time*1000:0):0;
    const closed=data.state==='closed';
    const verified = ['active', 'no_pass'].includes(data.state) && data.verified === true;
    const active = data.state === 'active';
    $('qrPhoneForm').hidden = verified||closed;
    $('qrCodeForm').hidden = true;
    $('qrOpen').hidden = !active;
    $('qrForget').hidden = !verified;
    $('qrPreferences').hidden = !verified || !data.preferences_url || data.is_temporary === true;
    if (data.preferences_url && !data.is_temporary) $('qrPreferences').href = data.preferences_url;
    $('qrBadge').textContent = closed?'Вне рабочих часов':active ? 'Пропуск действует' : verified ? 'Нет пропуска' : 'Вход по телефону';
    $('qrBadge').classList.toggle('active', active);
    $('qrHeading').textContent = closed?'Сейчас доступ закрыт':verified ? (active ? (data.user_name || 'Добро пожаловать') : 'Пропуск не действует') : 'Ваш номер телефона';
    $('qrDescription').textContent = closed?data.message:verified ? (active ? 'Вы рядом с воротами? Нажмите кнопку, чтобы открыть.' : 'Свяжитесь с администратором филиала, чтобы получить доступ.') : 'Проверим пропуск в этом филиале. При первом входе подтвердите номер кодом из SMS.';
    cooldownUntil = Math.max(cooldownUntil, Date.now() + (data.retry_after || 0) * 1000);
    expiresAt = data.expires_at ? Date.now() + Date.parse(data.expires_at) - (data.server_time ? data.server_time * 1000 : Date.now()) : 0;
    const pending = data.challenge;
    if (!closed && !verified && pending) {
      resendUntil = Date.now() + (pending.retry_after || 0) * 1000;
      if (pending.ready) showCode(pending);
    }
    tick();
  }
  function showCode(data) {
    challenge = data.challenge_id;
    $('qrPhoneForm').hidden = true;
    $('qrCodeForm').hidden = false;
    $('qrHeading').textContent = 'Подтвердите номер';
    $('qrDescription').textContent = 'Введите код из SMS на номер ' + data.phone_hint + '.';
    $('qrBadge').textContent = 'Проверка номера';
  }
  async function sendCode() {
    if (busy || currentState==='closed' || Date.now() < resendUntil) return;
    generation++;
    if (!$('qrPhone').value.trim()) {
      $('qrCodeForm').hidden = true;
      $('qrPhoneForm').hidden = false;
      $('qrPhone').focus();
      status('Введите номер ещё раз для повторной отправки.');
      return;
    }
    busy = true; tick(); status('Передаём SMS на отправку…');
    try {
      const data = await post('/code', {phone: $('qrPhone').value});
      if(data.state==='closed')render(data);
      resendUntil = Date.now() + (data.retry_after || 0) * 1000;
      if (data.ready) { showCode(data); $('qrCode').focus(); }
      status(data.message, !data.ready);
    } catch (error) {
      if (error.retryAfter) resendUntil = Date.now() + error.retryAfter * 1000;
      status(error.message, true);
    } finally { busy = false; tick(); }
  }
  $('qrPhoneForm').addEventListener('submit', event => { event.preventDefault(); sendCode(); });
  $('qrResend').addEventListener('click', sendCode);
  $('qrCodeForm').addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    generation++; busy = true; tick(); status('Проверяем код…');
    try { render(await post('/verify', {challenge_id: challenge, code: $('qrCode').value, remember: $('qrRemember').checked})); status('Номер подтверждён.'); }
    catch (error) { status(error.message, true); }
    finally { busy = false; tick(); }
  });
  $('qrOpen').addEventListener('click', async () => {
    if (busy || currentState !== 'active' || Date.now() < cooldownUntil) return;
    generation++; busy = true; cooldownUntil = Date.now() + 20000; tick(); status('Передаём команду…');
    try {
      const id = typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');
      const result = await post('/open', {request_id: id});
      status(result.message || (result.delivery === 'dry_run' || result.delivery === 'simulated' ? 'Тестовый режим: реальная команда не отправлена.' : 'Команда принята.'));
      cooldownUntil = Date.now() + (result.retry_after || 20) * 1000;
    } catch (error) {
      if (error.retryAfter) cooldownUntil = Date.now() + error.retryAfter * 1000;
      status(error.message + (error instanceof TypeError ? ' Результат неизвестен; команда автоматически не повторяется.' : ''), true);
    } finally { busy = false; tick(); }
  });
  $('qrForget').addEventListener('click', async () => {
    if (busy) return;
    generation++; busy = true; tick();
    try { await post('/forget'); $('qrPhone').value = ''; $('qrCode').value = ''; challenge = null; render(await post('/state')); status('Номер забыт на этом устройстве.'); }
    catch (error) { status(error.message, true); }
    finally { busy = false; tick(); }
  });
  $('qrRemember').addEventListener('change', () => {
    $('qrRememberHint').textContent = $('qrRemember').checked ? 'На этом устройстве на 30 дней.' : 'Только на эту сессию, максимум 12 часов. На общем устройстве нажмите «Использовать другой номер» после завершения.';
  });
  async function refresh() {
    if (busy || refreshing || document.hidden) return;
    const requestedGeneration = generation;
    refreshing = true;
    try { const data = await post('/state'); if (requestedGeneration === generation && !busy) render(data); }
    catch (error) { if (requestedGeneration === generation && !busy) { $('qrOpen').hidden = true; currentState = 'unavailable'; status(error.message, true); } }
    finally { refreshing = false; }
  }
  render(config);
  refresh();
  setInterval(tick, 250);
  setInterval(() => { if (['active', 'no_pass','closed'].includes(currentState)) refresh(); }, 10000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
})();
