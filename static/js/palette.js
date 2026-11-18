'use strict';
(() => {
  const page = document.getElementById('palettePage');
  if (!page) return;
  const $ = id => document.getElementById(id);
  const colors = $('paletteColors'), notice = $('paletteNotice');
  const inputs = {search: $('paletteSearch'), category: $('paletteCategory'), file: $('paletteFile'), changed: $('paletteOnlyChanged')};
  const preview = $('palettePreviewEnabled');
  const defaultsMode = page.dataset.defaults === 'true';
  const style = document.createElement('style');
  style.id = 'paletteDraftStyle';
  document.head.append(style);
  let model = null, entries = [], byId = new Map(), rootColors = new Map(), draft = {}, saved = {}, busy = false, shown = 40, resetRequested = false;
  const invalid = new Map();
  // Keep the restore controls legible while experimenting with the rest of this page.
  const toolbar = page.querySelector('.palette-toolbar');
  const baseline = getComputedStyle(document.documentElement);
  for (const variable of ['--ink', '--muted', '--line', '--surface', '--background', '--accent', '--accent-hover', '--secondary', '--lavender']) {
    const value = baseline.getPropertyValue(variable).trim();
    if (value) toolbar.style.setProperty(variable, value);
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function normalized(value) {
    if (typeof value !== 'string') return null;
    let hex = value.trim().replace(/^#/, '').toUpperCase();
    if (!/^(?:[\dA-F]{3}|[\dA-F]{4}|[\dA-F]{6}|[\dA-F]{8})$/.test(hex)) return null;
    if (hex.length <= 4) hex = [...hex].map(ch => ch + ch).join('');
    if (hex.length === 8 && hex.endsWith('FF')) hex = hex.slice(0, 6);
    return '#' + hex;
  }
  function colorParts(value) {
    const color = normalized(value) || '#000000';
    return {color, rgb: color.slice(0, 7), alpha: color.length === 9 ? parseInt(color.slice(7), 16) : 255};
  }
  function withAlpha(rgb, alpha) {
    const hex = Math.round(Math.max(0, Math.min(255, alpha))).toString(16).padStart(2, '0').toUpperCase();
    return normalized(rgb.slice(0, 7) + hex);
  }
  function aliasName(entry) { return /^\s*var\(\s*(--[\w-]+)\s*\)\s*$/.exec(entry.source_value || '')?.[1]; }
  function effective(entry, visited = new Set()) {
    if (Object.hasOwn(draft, entry.id)) return draft[entry.id];
    const fallback = normalized(entry.default) || '#000000';
    if (visited.has(entry.id)) return fallback;
    visited.add(entry.id);
    const parent = rootColors.get(aliasName(entry));
    return parent ? effective(parent, visited) : fallback;
  }
  function changedKeys() {
    return [...new Set([...Object.keys(saved), ...Object.keys(draft)])].filter(key => saved[key] !== draft[key]);
  }
  function tell(message, error = false, reload = false) {
    notice.replaceChildren(document.createTextNode(message));
    notice.dataset.error = String(error); notice.hidden = false;
    if (reload) {
      const button = element('button', 'palette-conflict-reload', 'Загрузить сохранённую палитру');
      button.type = 'button';
      button.addEventListener('click', () => {
        if ((changedKeys().length || resetRequested) && !window.confirm('Загрузить сохранённую палитру и отменить ваш черновик?')) return;
        action(async () => { install(await request('/admin/api/palette')); await refreshSavedStyles(); applyPreview(); tell('Сохранённая палитра загружена'); });
      });
      notice.append(button);
    }
  }
  function state() {
    const changes = changedKeys().length;
    $('paletteChangeCount').textContent = invalid.size ? 'Проверьте значения цветов' : changes ? 'Изменено цветов: ' + changes : resetRequested ? 'Выбрана стандартная палитра' : 'Все изменения сохранены';
    $('paletteSave').disabled = busy || !model || (!changes && !resetRequested) || invalid.size > 0;
    $('paletteDiscard').disabled = busy || !model || (!changes && !invalid.size && !resetRequested);
    $('paletteResetAll').disabled = busy || !model || (Object.keys(draft).length === 0 && !model.unused_overrides);
    $('paletteExport').disabled = busy || !model || invalid.size > 0;
    $('paletteImport').disabled = busy || !model;
    colors.querySelectorAll('input,button').forEach(node => {
      const id = node.closest('[data-color-id]')?.dataset.colorId;
      node.disabled = busy || byId.get(id)?.editable === false || (node.dataset.action === 'reset-color' && !Object.hasOwn(draft, id));
    });
  }
  function applyPreview() {
    // initial makes var(--token, ORIGINAL) resolve its fallback at the original
    // element; resolving aliases/dynamic colors at :root would change their meaning.
    style.textContent = preview.checked && model ? ':root{' + entries.map(entry => entry.css_var + ':' + (draft[entry.id] || 'initial')).join(';') + '}' : '';
  }
  function valueChanged(entry, value, card, origin) {
    const hex = normalized(value);
    if (!hex) return;
    draft[entry.id] = hex;
    resetRequested = false;
    invalid.delete(entry.id);
    updateCard(card, entry, origin);
    applyPreview(); refreshDerivedCards(card); state();
  }
  function updateCard(card, entry, origin) {
    const parts = colorParts(effective(entry));
    card.dataset.configured = String(Object.hasOwn(draft, entry.id));
    card.querySelector('.palette-swatch span').style.backgroundColor = parts.color;
    const picker = card.querySelector('[data-field=picker]'), hex = card.querySelector('[data-field=hex]'), alpha = card.querySelector('[data-field=alpha]');
    if (picker !== origin) picker.value = parts.rgb;
    if (hex !== origin) hex.value = parts.color;
    if (alpha !== origin) alpha.value = String(Math.round(parts.alpha / 255 * 1000) / 10);
    for (const input of [hex, alpha]) input.setAttribute('aria-invalid', 'false');
    card.querySelector('.palette-color-error').hidden = true;
    card.querySelector('.palette-card-indicator').hidden = !Object.hasOwn(draft, entry.id);
    card.querySelector('[data-action=reset-color]').disabled = busy || !Object.hasOwn(draft, entry.id);
  }
  function cardError(entry, card, input, message) {
    invalid.set(entry.id, {field: input.dataset.field, value: input.value, message}); input.setAttribute('aria-invalid', 'true');
    const error = card.querySelector('.palette-color-error'); error.hidden = false; error.textContent = message;
    state();
  }
  function refreshDerivedCards(origin) {
    for (const card of colors.querySelectorAll('[data-color-id]')) {
      const entry = byId.get(card.dataset.colorId);
      if (!entry || card === origin || card.contains(document.activeElement) || invalid.has(entry.id) || Object.hasOwn(draft, entry.id) || !aliasName(entry)) continue;
      updateCard(card, entry);
    }
  }
  function colorCard(entry) {
    const card = element('article', 'palette-color-card'); card.dataset.colorId = entry.id;
    const heading = element('div', 'palette-card-heading'), swatch = element('div', 'palette-swatch'); swatch.setAttribute('aria-hidden', 'true'); swatch.append(element('span'));
    const title = element('div', 'palette-card-title'); title.append(element('h3', '', entry.label || entry.css_var), element('p', '', entry.group || 'Цвет интерфейса'));
    if (entry.status_color) title.append(element('span', 'palette-color-kind', 'Информационный цвет · статус'));
    const indicator = element('span', 'palette-card-indicator'); indicator.title = 'Цвет настроен вручную';
    heading.append(swatch, title, indicator); card.append(heading);
    const controls = element('div', 'palette-color-controls');
    const pickerLabel = element('label', '', 'Цвет'), picker = element('input'); picker.type = 'color'; picker.dataset.field = 'picker'; picker.setAttribute('aria-label', 'Выбрать цвет: ' + entry.label);
    const hexLabel = element('label', '', 'HEX'), hex = element('input'); hex.type = 'text'; hex.dataset.field = 'hex'; hex.maxLength = 9; hex.autocomplete = 'off'; hex.spellcheck = false; hex.setAttribute('aria-label', 'HEX: ' + entry.label);
    const alphaLabel = element('label', '', 'Непрозр., %'), alpha = element('input'); alpha.type = 'number'; alpha.dataset.field = 'alpha'; alpha.min = '0'; alpha.max = '100'; alpha.step = '0.1'; alpha.setAttribute('aria-label', 'Непрозрачность: ' + entry.label);
    pickerLabel.append(picker); hexLabel.append(hex); alphaLabel.append(alpha); controls.append(pickerLabel, hexLabel, alphaLabel); card.append(controls);
    const error = element('p', 'palette-color-error'); error.hidden = true; error.setAttribute('role', 'status'); card.append(error);
    const defaults = element('div', 'palette-default-row'), source = element('small', '', 'Исходный: ' + (entry.default || entry.source_value));
    source.title = entry.source_value || '';
    const reset = element('button', '', 'Сбросить'); reset.type = 'button'; reset.dataset.action = 'reset-color'; reset.setAttribute('aria-label', 'Сбросить цвет: ' + entry.label); defaults.append(source, reset); card.append(defaults);
    if (entry.dynamic || /var\(|currentcolor/i.test(entry.source_value || '')) card.append(element('p', 'palette-color-kind', 'Связанный или динамический цвет. Без настройки сохраняется исходная зависимость.'));
    if (entry.legacy) card.append(element('p', 'palette-color-kind', 'Стили прежних страниц. На современных страницах могут не использоваться.'));
    const usage = element('details', 'palette-usage'), locations = entry.locations || [];
    usage.append(element('summary', '', 'Где применяется · ' + locations.length));
    usage.addEventListener('toggle', () => {
      if (!usage.open || usage.dataset.built) return;
      usage.dataset.built = 'true'; const list = element('ul', 'palette-usage-list');
      for (const location of locations) {
        const row = element('li'); row.append(element('strong', '', location.file + (location.line ? ':' + location.line : '')));
        row.append(element('code', '', location.selector || 'Общий цвет'));
        row.append(element('code', '', location.property || ''));
        if (location.context) row.append(element('span', 'palette-usage-context', Array.isArray(location.context) ? location.context.join(' · ') : location.context));
        list.append(row);
      }
      if (!locations.length) list.append(element('li', '', 'Общая переменная палитры'));
      usage.append(list);
    });
    card.append(usage);
    picker.addEventListener('input', () => valueChanged(entry, withAlpha(picker.value, colorParts(effective(entry)).alpha), card, picker));
    picker.addEventListener('change', () => valueChanged(entry, withAlpha(picker.value, colorParts(effective(entry)).alpha), card, picker));
    hex.addEventListener('input', () => {
      const value = normalized(hex.value);
      if (value) valueChanged(entry, value, card, hex);
      else cardError(entry, card, hex, 'Укажите HEX: #RGB, #RRGGBB или #RRGGBBAA.');
    });
    hex.addEventListener('blur', () => { if (normalized(hex.value)) updateCard(card, entry); });
    alpha.addEventListener('input', () => {
      const value = Number(alpha.value);
      if (alpha.value.trim() !== '' && Number.isFinite(value) && value >= 0 && value <= 100) valueChanged(entry, withAlpha(colorParts(effective(entry)).rgb, value / 100 * 255), card, alpha);
      else cardError(entry, card, alpha, 'Непрозрачность — число от 0 до 100.');
    });
    reset.addEventListener('click', () => { delete draft[entry.id]; invalid.delete(entry.id); updateCard(card, entry); applyPreview(); refreshDerivedCards(card); state(); });
    updateCard(card, entry);
    if (invalid.has(entry.id)) {
      const problem = invalid.get(entry.id), input = card.querySelector('[data-field="' + problem.field + '"]');
      input.value = problem.value; input.setAttribute('aria-invalid', 'true');
      error.hidden = false; error.textContent = problem.message;
    }
    return card;
  }
  function render() {
    if (!model) return;
    const query = inputs.search.value.trim().toLocaleLowerCase();
    const filtered = entries.filter(entry => {
      if (inputs.category.value && entry.group !== inputs.category.value) return false;
      if (inputs.file.value && !(entry.locations || []).some(item => item.file === inputs.file.value)) return false;
      if (inputs.changed.checked && !Object.hasOwn(draft, entry.id)) return false;
      return !query || entry._search.includes(query) || effective(entry).toLocaleLowerCase().includes(query);
    });
    colors.replaceChildren(...filtered.slice(0, shown).map(colorCard)); colors.setAttribute('aria-busy', 'false');
    $('paletteResultCount').textContent = 'Цвета · ' + filtered.length;
    $('paletteCatalogHint').textContent = filtered.length ? 'Показано ' + Math.min(shown, filtered.length) + ' из ' + filtered.length : '';
    $('paletteEmpty').hidden = filtered.length !== 0;
    $('paletteLoadMore').hidden = shown >= filtered.length;
    state();
  }
  function install(data) {
    model = data;
    entries = (data.entries || []).map(entry => ({...entry, _search: [entry.label, entry.css_var, entry.source_value, entry.default, entry.group,
      ...(entry.locations || []).flatMap(location => [location.file, location.selector, location.property, location.context])].join(' ').toLocaleLowerCase()}));
    byId = new Map(entries.map(entry => [entry.id, entry]));
    rootColors = new Map();
    for (const entry of entries) {
      const location = entry.locations?.[0];
      if (entry.active && location?.selector === ':root' && !location.context && location.property?.startsWith('--')) rootColors.set(location.property, entry);
    }
    saved = Object.fromEntries(Object.entries(data.overrides || {}).map(([key, value]) => [key, normalized(value)]));
    draft = {...saved}; invalid.clear(); resetRequested = false;
    for (const [select, values, title] of [[inputs.category, new Set(entries.map(entry => entry.group).filter(Boolean)), 'Все разделы'],
      [inputs.file, new Set(entries.flatMap(entry => (entry.locations || []).map(location => location.file))), 'Все файлы']]) {
      const selected = select.value;
      const all = element('option', '', title); all.value = ''; select.replaceChildren(all);
      for (const value of [...values].sort((a, b) => a.localeCompare(b, 'ru'))) { const option = element('option', '', value); option.value = value; select.append(option); }
      if (values.has(selected)) select.value = selected;
    }
    $('paletteTotal').textContent = entries.length + ' цветов';
    const stamp = data.updated_at ? new Date(typeof data.updated_at === 'number' ? data.updated_at * 1000 : data.updated_at) : null;
    $('paletteSavedAt').textContent = stamp && !Number.isNaN(stamp.valueOf()) ? 'Сохранено ' + stamp.toLocaleString('ru-RU') : 'Используется исходная палитра GateBot';
    applyPreview(); render();
  }
  async function request(path, data) {
    const options = {credentials: 'same-origin', headers: {'Accept': 'application/json'}};
    if (data !== undefined) { options.method = 'POST'; options.headers['Content-Type'] = 'application/json'; options.headers['X-CSRF-Token'] = document.querySelector('meta[name=csrf-token]').content; options.body = JSON.stringify(data); }
    const response = await fetch(path, options);
    let result; try { result = await response.json(); } catch { throw new Error('Нет ответа сервера. Черновик сохранён на этой странице.'); }
    if (!response.ok) { const error = new Error(result.message || 'Не удалось сохранить палитру'); error.conflict = response.status === 409; throw error; }
    return result;
  }
  async function action(callback) {
    if (busy) return;
    busy = true; state();
    try { await callback(); }
    catch (error) { tell(error.message || 'Не удалось выполнить действие', true, error.conflict); }
    finally { busy = false; state(); }
  }
  async function refreshSavedStyles() {
    if (defaultsMode) return;
    const link = [...document.querySelectorAll('link[rel=stylesheet]')].find(item => new URL(item.href, location.href).pathname === '/ui/palette.css');
    if (!link) return;
    await new Promise(resolve => {
      let finished = false;
      const finish = () => { if (finished) return; finished = true; link.removeEventListener('load', finish); link.removeEventListener('error', finish); resolve(); };
      link.addEventListener('load', finish); link.addEventListener('error', finish);
      link.href = '/ui/palette.css?revision=' + encodeURIComponent(model.revision);
      setTimeout(finish, 3000);
    });
  }
  $('paletteSave').addEventListener('click', () => action(async () => {
    if (invalid.size || !model) return;
    const result = resetRequested && Object.keys(draft).length === 0
      ? await request('/admin/api/palette/reset', {catalog_version: model.catalog_version, revision: model.revision})
      : await request('/admin/api/palette', {catalog_version: model.catalog_version, revision: model.revision, overrides: draft});
    install(result); await refreshSavedStyles(); applyPreview();
    tell('Палитра сохранена. Новые цвета появятся на страницах всех филиалов после обновления.');
  }));
  $('paletteDiscard').addEventListener('click', () => { draft = {...saved}; invalid.clear(); resetRequested = false; applyPreview(); render(); tell('Несохранённые изменения отменены'); });
  preview.addEventListener('change', applyPreview);
  for (const input of Object.values(inputs)) input.addEventListener(input.type === 'search' ? 'input' : 'change', () => { shown = 40; render(); });
  $('paletteLoadMore').addEventListener('click', () => { shown += 40; render(); });
  $('paletteClearFilters').addEventListener('click', () => { inputs.search.value = inputs.category.value = inputs.file.value = ''; inputs.changed.checked = false; shown = 40; render(); });
  $('paletteResetAll').addEventListener('click', () => $('paletteResetDialog').showModal());
  $('paletteCancelReset').addEventListener('click', () => $('paletteResetDialog').close());
  $('paletteConfirmReset').addEventListener('click', () => {
    $('paletteResetDialog').close(); draft = {}; invalid.clear(); resetRequested = true; applyPreview(); render();
    tell('Выбрана исходная палитра. Нажмите «Сохранить палитру», чтобы применить её ко всему сайту.');
  });
  $('paletteExport').addEventListener('click', () => {
    if (!model || invalid.size) return;
    const blob = new Blob([JSON.stringify({format: 'gatebot-palette-v1', catalog_version: model.catalog_version, overrides: draft}, null, 2) + '\n'], {type: 'application/json'});
    const url = URL.createObjectURL(blob), anchor = element('a'); anchor.href = url; anchor.download = 'gatebot-palette.json'; document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  $('paletteImport').addEventListener('click', () => $('paletteImportFile').click());
  $('paletteImportFile').addEventListener('change', event => action(async () => {
    const file = event.target.files[0]; event.target.value = ''; if (!file || !model) return;
    if (file.size > 256 * 1024) throw new Error('Конфиг слишком большой. Максимум — 256 КБ.');
    let data; try { data = JSON.parse(await file.text()); } catch { throw new Error('Не удалось прочитать JSON-конфиг.'); }
    if (!data || Array.isArray(data) || data.format !== 'gatebot-palette-v1' || data.catalog_version !== model.catalog_version ||
        !data.overrides || typeof data.overrides !== 'object' || Array.isArray(data.overrides) || Object.keys(data).some(key => !['format', 'catalog_version', 'overrides'].includes(key))) {
      throw new Error('Это не конфиг текущей версии палитры GateBot. Скачайте актуальный конфиг.');
    }
    const imported = {};
    for (const [id, value] of Object.entries(data.overrides)) {
      if (!byId.has(id) || byId.get(id).editable === false || !normalized(value)) throw new Error('Конфиг содержит неизвестный цвет или неверное HEX-значение.');
      imported[id] = normalized(value);
    }
    draft = imported; invalid.clear(); resetRequested = false; applyPreview(); render();
    tell('Конфиг загружен в черновик. Проверьте цвета и нажмите «Сохранить палитру».');
  }));
  window.addEventListener('beforeunload', event => {
    if (!changedKeys().length && !invalid.size && !resetRequested) return;
    event.preventDefault(); event.returnValue = '';
  });
  action(async () => { install(await request('/admin/api/palette')); });
})();
