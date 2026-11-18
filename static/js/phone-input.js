/* Phone input is presentation only; the server validates and normalizes again. */
(() => {
  'use strict';
  const selector = 'input[type="tel"],input[data-phone]';
  function normalize(value) {
    const raw = String(value || '').trim();
    if (!raw || !/^[+\d\s()\-–—.]+$/.test(raw)) return raw;
    const compact = raw.replace(/[\s()\-–—.]/g, '');
    if (!/^\+?\d+$/.test(compact)) return raw;
    const digits = compact.replace(/^\+/, '');
    if (!compact.startsWith('+') && /^8\d{10}$/.test(digits)) return '+7' + digits.slice(1);
    if (!compact.startsWith('+') && /^9\d{9}$/.test(digits)) return '+7' + digits;
    if (/^[1-9]\d{6,14}$/.test(digits)) return '+' + digits;
    return raw;
  }
  function update(input) {
    if (!(input instanceof HTMLInputElement) || !input.matches(selector) || input.disabled || input.readOnly) return;
    const normalized = normalize(input.value);
    if (normalized !== input.value) {
      input.value = normalized;
      input.dispatchEvent(new Event('input', {bubbles: true}));
    }
  }
  function normalizeForm(form) {
    form?.querySelectorAll(selector).forEach(update);
  }
  document.addEventListener('focusout', event => update(event.target), true);
  document.addEventListener('change', event => update(event.target), true);
  document.addEventListener('input', event => {
    if (event.target.matches?.(selector)) {
      event.target.setCustomValidity('');
      if (event.inputType === 'insertFromPaste') update(event.target);
    }
  }, true);
  document.addEventListener('submit', event => normalizeForm(event.target), true);
  // OTP and dialog buttons may submit data without triggering a form submit.
  document.addEventListener('click', event => {
    const control = event.target.closest?.('button,input[type="submit"]');
    if (control) normalizeForm(control.closest('form') || control.closest('.modal,dialog'));
  }, true);
  window.GateBotPhone = Object.freeze({normalize, normalizeForm});
})();
