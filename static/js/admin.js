// Tenant authorization lives on the server. This UI never treats hidden controls as permissions.
'use strict';

let usersCache = [];
let sortState = { key: null, asc: true };

function element(tag, className = '', text = null) {
    const value = document.createElement(tag);
    if (className) value.className = className;
    if (text !== null) value.textContent = String(text);
    return value;
}

function showAlert(message, type = 'info') {
    const banner = element('div', 'alert alert-' + type, message);
    const container = document.querySelector('.admin-content') || document.body;
    container.prepend(banner);
    window.setTimeout(() => banner.remove(), 7000);
}

async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.method && !['GET', 'HEAD'].includes(options.method.toUpperCase())) {
        headers.set('X-CSRF-Token', document.querySelector('meta[name="csrf-token"]')?.content || '');
        headers.set('Content-Type', 'application/json');
    }
    const response = await fetch(path, { ...options, headers, credentials: 'same-origin' });
    let data;
    try { data = await response.json(); }
    catch { throw new Error('Сервер вернул неожиданный ответ. Обновите страницу.'); }
    if (response.status === 401) {
        window.location.assign('/admin/login');
        throw new Error('Войдите в систему заново');
    }
    if (!response.ok) throw new Error(data.message || 'Не удалось выполнить действие');
    if (data.csrf_token) {
        document.querySelector('meta[name="csrf-token"]').content = data.csrf_token;
        document.querySelectorAll('input[name="csrf_token"]').forEach(input => { input.value = data.csrf_token; });
    }
    return data;
}

function post(path, payload) {
    return api(path, { method: 'POST', body: JSON.stringify(payload) });
}

async function copyToClipboard(text, showNotification = true) {
    let copied = false;
    if (navigator.clipboard?.writeText) {
        try { await navigator.clipboard.writeText(text); copied = true; }
        catch { /* Local HTTP pages can use the explicit selection fallback. */ }
    }
    if (!copied) {
        const area = element('textarea');
        area.value = text;
        area.style.position = 'fixed';
        area.style.left = '-9999px';
        document.body.append(area);
        area.focus();
        area.select();
        try { copied = document.execCommand('copy'); } catch { copied = false; }
        area.remove();
    }
    if (showNotification) showAlert(copied ? 'Ссылка скопирована' : 'Выделите и скопируйте ссылку вручную', copied ? 'success' : 'info');
    return copied;
}

function fullLink(path) {
    return String(window.BASE_URL || window.location.origin).replace(/\/$/, '') + path;
}

const ICONS = {
    copy: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="9" y="9" width="13" height="13" rx="2" stroke="#272b2e" stroke-width="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" stroke="#272b2e" stroke-width="2"/></svg>',
    edit: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M3 17v4h4L20 8l-4-4L3 17z" stroke="#272b2e" stroke-width="2"/></svg>',
    delete: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M9 3h6M4 6h16M6 6l1 14h10l1-14M10 10v8M14 10v8" stroke="#272b2e" stroke-width="2"/></svg>'
};

function button(text, callback, className = 'btn-action') {
    const value = element('button', className, text);
    value.type = 'button';
    value.addEventListener('click', callback);
    return value;
}

function iconButton(title, icon, callback) {
    const value = button('', callback, 'btn-icon');
    value.title = title;
    value.setAttribute('aria-label', title);
    // Only these static, source-controlled SVG constants are interpreted as markup.
    value.innerHTML = ICONS[icon] || '';
    return value;
}

function createModal(title) {
    const modal = element('div', 'modal');
    modal.style.display = 'block';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.setAttribute('aria-label', title);
    const content = element('div', 'modal-content');
    content.append(element('h2', '', title));
    const body = element('div', 'modal-form-content');
    const actions = element('div', 'modal-buttons');
    content.append(body, actions);
    modal.append(content);
    document.body.append(modal);
    return { modal, body, actions, close: () => modal.remove() };
}

function formField(body, label, value, type = 'text', readOnly = false) {
    const group = element('div', 'form-group');
    const labelNode = element('label', '', label);
    const input = element('input');
    input.type = type;
    input.value = value ?? '';
    input.readOnly = readOnly;
    labelNode.append(input);
    group.append(labelNode);
    body.append(group);
    return input;
}

function passInfo(user) {
    if (!user.gate_access) return { category: 'Запрещен', text: 'Запрещен', className: 'btn-pass-denied' };
    if (!user.access_expires) return { category: 'Постоянный', text: 'Постоянный', className: 'btn-pass-permanent' };
    const days = Number(user.access_expires_days);
    if (days <= 1) return { category: 'Разовый', text: 'Разовый', className: 'btn-pass-one' };
    const last = days % 10;
    const lastTwo = days % 100;
    const word = lastTwo >= 11 && lastTwo <= 19 ? 'дней' : (last === 1 ? 'день' : (last >= 2 && last <= 4 ? 'дня' : 'дней'));
    return { category: 'Временный', text: String(days) + ' ' + word, className: 'btn-pass-multi' };
}

async function loadUsers() {
    try {
        const data = await api('/admin/api/users');
        usersCache = data.users || [];
        filterUsers();
    } catch (error) { showAlert(error.message, 'error'); }
}

function displayUsers(users) {
    const tbody = document.getElementById('usersTableBody');
    tbody.replaceChildren();
    users.forEach(user => {
        const row = element('tr');
        const pass = passInfo(user);
        row.dataset.pass = pass.category;
        function cell(label, text = null) {
            const td = element('td', '', text);
            td.dataset.label = label;
            row.append(td);
            return td;
        }
        cell('Имя', user.user_name || '—');
        cell('Телефон', user.phone || '—');
        cell('Филиал', user.branch_name || '—');
        cell('Пропуск').append(button(pass.text, () => onPassButtonClick(user.token, !!user.gate_access), 'btn-pass ' + pass.className));
        cell('Боты', [user.has_telegram ? 'TG' : '', user.has_max ? 'MAX' : ''].filter(Boolean).join(' · ') || '—');
        cell('YCLIENTS', user.has_yclients ? '✓' : '');
        const mailing = element('input');
        mailing.type = 'checkbox';
        mailing.checked = !!user.mailing_enabled;
        mailing.disabled = !user.has_yclients;
        mailing.title = user.has_yclients ? 'Уведомления о пропуске' : 'Нет клиента в YCLIENTS';
        mailing.addEventListener('change', () => onToggleMailing(user.token, mailing.checked));
        cell('Рассылка').append(mailing);
        const actions = cell('Действия');
        actions.style.whiteSpace = 'nowrap';
        actions.style.textAlign = 'right';
        actions.append(
            iconButton('Копировать ссылку', 'copy', () => onCopyUserLink(user.token)),
            iconButton('Редактировать', 'edit', () => openEditUserModal(user.token)),
            iconButton('Удалить', 'delete', () => onDeleteUser(user.token))
        );
        tbody.append(row);
    });
}

function filterUsers() {
    const search = (document.getElementById('searchInput')?.value || '').toLowerCase();
    const branchId = document.getElementById('filterBranch')?.value || '';
    const status = document.getElementById('filterStatus')?.value || '';
    let users = usersCache.filter(user => {
        return (!search || String(user.user_name || '').toLowerCase().includes(search) || String(user.phone || '').includes(search)) &&
            (!branchId || String(user.branch_id) === branchId) && (!status || passInfo(user).category === status);
    });
    if (sortState.key) {
        users = [...users].sort((a, b) => {
            const getValue = u => sortState.key === 'pass_label' ? passInfo(u).text : (u[sortState.key] ?? '');
            return String(getValue(a)).localeCompare(String(getValue(b)), 'ru', { numeric: true }) * (sortState.asc ? 1 : -1);
        });
    }
    displayUsers(users);
}

function sortUsersBy(key) {
    sortState = { key, asc: sortState.key === key ? !sortState.asc : true };
    filterUsers();
}

async function loadBranches() {
    try {
        const data = await api('/admin/api/branches');
        const branches = data.branches || [];
        document.querySelectorAll('select[name="branchId"]').forEach(select => {
            select.replaceChildren();
            branches.forEach(branch => {
                const option = element('option', '', branch.name);
                option.value = String(branch.id);
                select.append(option);
            });
        });
        const filter = document.getElementById('filterBranch');
        if (filter) {
            const all = element('option', '', 'Все филиалы');
            all.value = '';
            filter.replaceChildren(all);
            branches.forEach(branch => {
                const option = element('option', '', branch.name);
                option.value = String(branch.id);
                filter.append(option);
            });
        }
    } catch (error) { showAlert(error.message, 'error'); }
}

function openEditUserModal(token) {
    const user = usersCache.find(value => value.token === token);
    if (!user) return;
    const view = createModal('Редактировать пользователя');
    const name = formField(view.body, 'Имя', user.user_name);
    const phone = formField(view.body, 'Телефон', user.phone, 'tel');
    const crm = formField(view.body, 'YCLIENTS ID', user.id_ycl);
    formField(view.body, 'Telegram — подтвержденная привязка', user.tg_id || 'Не привязан', 'text', true);
    formField(view.body, 'MAX — подтвержденная привязка', user.max_id || 'Не привязан', 'text', true);
    view.body.append(element('p', 'form-hint', 'После смены телефона пользователь заново привязывает мессенджер по персональной ссылке или своему контакту.'));
    const save = button('Сохранить', async () => {
        save.disabled = true;
        try {
            await post('/admin/api/update-user', { token, user_name: name.value, phone: phone.value, id_ycl: crm.value });
            view.close();
            await loadUsers();
            showAlert('Изменения сохранены', 'success');
        } catch (error) { showAlert(error.message, 'error'); }
        finally { save.disabled = false; }
    });
    view.actions.append(button('Отмена', view.close), save);
}

async function toggleAccess(token, enable) {
    try {
        await post('/admin/api/toggle-access', { token, enable });
        showAlert('Доступ обновлен', 'success');
        await loadUsers();
    } catch (error) { showAlert(error.message, 'error'); }
}

function onPassButtonClick(token, accessAllowed) {
    if (!accessAllowed) { showAccessModal(token); return; }
    const view = createModal('Управление пропуском');
    view.body.append(element('p', '', 'Запретить доступ пользователю? Автоматическая синхронизация не отменит этот запрет.'));
    view.actions.append(button('Отмена', view.close), button('Запретить', () => { view.close(); toggleAccess(token, false); }, 'btn-action btn-deny'));
}

function showAccessModal(token) {
    document.getElementById('tokenField').value = token;
    document.getElementById('accessModal').style.display = 'block';
}

function closeModal() {
    document.getElementById('accessModal').style.display = 'none';
}

async function onToggleMailing(token, enabled) {
    try {
        await post('/admin/api/set-mailing', { token, enabled });
        showAlert('Настройка уведомлений обновлена', 'success');
    } catch (error) { showAlert(error.message, 'error'); }
    await loadUsers();
}

function onDeleteUser(token) {
    const view = createModal('Удалить пользователя?');
    view.body.append(element('p', '', 'Пропуск и привязки мессенджеров будут удалены.'));
    const confirm = button('Удалить', async () => {
        confirm.disabled = true;
        try {
            await post('/admin/api/delete-user', { token });
            view.close();
            await loadUsers();
            showAlert('Пользователь удален', 'success');
        } catch (error) { showAlert(error.message, 'error'); }
        finally { confirm.disabled = false; }
    }, 'btn-action btn-deny');
    view.actions.append(button('Отмена', view.close), confirm);
}

function showLinkModal(title, token, link, delivery = null) {
    const view = createModal(title);
    const box = element('div', 'link-box');
    const input = element('input');
    input.type = 'text';
    input.value = link;
    input.readOnly = true;
    box.append(input, iconButton('Копировать ссылку', 'copy', () => copyToClipboard(link)));
    view.body.append(box);
    if (delivery && !['sent', 'not_requested'].includes(delivery.status)) {
        view.body.append(element('p', 'alert alert-info', 'Пропуск создан. Ссылка не отправлена: ' + delivery.message));
    }
    if (token) view.actions.append(button('Отправить ссылку', () => { view.close(); openSendLinkModal(token); }));
    view.actions.append(button('Закрыть', view.close));
    return view;
}

function onCopyUserLink(token) {
    const link = fullLink('/opn/' + encodeURIComponent(token));
    copyToClipboard(link, false);
    showLinkModal('Персональная ссылка', token, link);
}

function copyLink() {
    return copyToClipboard(document.getElementById('generatedLink').value);
}

function openSendLinkModal(token) {
    const user = usersCache.find(value => value.token === token) || {};
    const view = createModal('Отправить ссылку');
    view.body.append(element('p', '', 'Ссылка будет передана подключенному сервису уведомлений.'));
    const phone = formField(view.body, 'Номер телефона', user.phone || '', 'tel');
    const confirm = button('Отправить', async () => {
        confirm.disabled = true;
        try {
            const result = await post('/admin/api/send-link', { token, phone: phone.value });
            view.close();
            showAlert(result.message || 'Сообщение принято сервисом отправки', 'success');
            if (document.getElementById('usersTableBody')) await loadUsers();
        } catch (error) { showAlert(error.message, 'error'); }
        finally { confirm.disabled = false; }
    });
    view.actions.append(button('Отмена', view.close), confirm);
}

function notifyDelivery(delivery) {
    if (delivery?.status === 'sent') showAlert(delivery.message, 'success');
    else if (delivery && delivery.status !== 'not_requested') showAlert('Ссылка создана, но не отправлена: ' + delivery.message, 'info');
}

function checkPasswordStrength() {
    const password = document.getElementById('newPassword').value;
    const valid = password.length >= 12 && password.length <= 1024;
    const requirement = document.getElementById('length');
    requirement.classList.toggle('valid', valid);
    requirement.classList.toggle('invalid', !valid);
    checkPasswordMatch();
}

function checkPasswordMatch() {
    const password = document.getElementById('newPassword').value;
    const confirmation = document.getElementById('confirmPassword').value;
    const match = document.getElementById('passwordMatch');
    const equal = password === confirmation && password.length >= 12 && password.length <= 1024;
    match.textContent = confirmation ? (password === confirmation ? 'Пароли совпадают' : 'Пароли не совпадают') : '';
    match.className = 'password-match ' + (equal ? 'valid' : 'invalid');
    document.getElementById('submitBtn').disabled = !equal;
}

async function loadLogs() {
    const list = document.getElementById('logsList');
    list.replaceChildren();
    const query = new URLSearchParams();
    query.set('type', document.getElementById('logType').value);
    const from = document.getElementById('dateFrom').value;
    const to = document.getElementById('dateTo').value;
    if (from) query.set('from', from);
    if (to) query.set('to', to);
    try {
        const data = await api('/admin/api/logs?' + query.toString());
        const logs = data.logs || [];
        document.getElementById('noLogs').style.display = logs.length ? 'none' : 'block';
        logs.forEach(log => {
            const item = element('div', 'log-item');
            const header = element('div', 'log-header');
            const levelClass = ['ERROR', 'CRITICAL'].includes(log.level) ? 'error' : 'info';
            header.append(element('span', 'log-time', log.time), element('span', 'log-type ' + levelClass, log.level));
            item.append(header, element('div', 'log-message', log.message),
                element('div', 'log-details', 'Источник: ' + log.logger + (log.correlation_id ? ' · Запрос: ' + log.correlation_id : '')));
            list.append(item);
        });
    } catch (error) { showAlert(error.message, 'error'); }
}

function toggleBurgerMenu() {
    document.getElementById('navItems')?.classList.toggle('active');
}

async function initializeBranchSettings() {
    const form = document.getElementById('branchSettingsForm');
    if (!form) return;
    const selector = document.getElementById('settingsBranch');
    const rows = document.getElementById('settingsButtons');
    const branchId = document.getElementById('settingsBranchId');
    let creating = false;
    let loadSerial = 0;
    form.dataset.ready = 'false';
    function addRow(value = { type: 'gate', label: 'Въезд', phone: '' }) {
        if (rows.children.length >= 12) { showAlert('Можно добавить до 12 кнопок', 'info'); return; }
        const row = element('tr');
        function cell(label, control) {
            const td = element('td');
            td.dataset.label = label;
            td.append(control);
            row.append(td);
        }
        const type = element('select');
        type.dataset.field = 'type';
        [['gate', 'Открыть ворота'], ['admin', 'Позвонить администратору']].forEach(([code, label]) => {
            const option = element('option', '', label);
            option.value = code;
            type.append(option);
        });
        type.value = value.type;
        const label = element('input');
        label.dataset.field = 'label';
        label.value = value.label;
        label.required = true;
        label.maxLength = 100;
        const phone = element('input');
        phone.type = 'tel';
        phone.dataset.field = 'phone';
        phone.value = value.phone;
        phone.required = true;
        phone.maxLength = 40;
        phone.placeholder = '+7…';
        [type, label, phone].forEach(control => {
            control.style.width = '100%';
            control.style.padding = '8px';
        });
        cell('Действие', type);
        cell('Название кнопки', label);
        cell('Телефон', phone);
        cell('Удалить', iconButton('Удалить кнопку', 'delete', () => row.remove()));
        rows.append(row);
    }
    function fill(value) {
        branchId.value = value.branch_id || '';
        branchId.readOnly = !creating;
        document.getElementById('settingsName').value = value.name || '';
        document.getElementById('settingsTimezone').value = value.timezone || 'Europe/Moscow';
        document.getElementById('settingsLogo').value = value.logo || '';
        document.getElementById('settingsEnabled').checked = value.enabled === true;
        rows.replaceChildren();
        (value.buttons || []).forEach(addRow);
        document.getElementById('saveSettingsButton').textContent = creating ? 'Создать филиал' : 'Сохранить настройки';
        form.dataset.ready = 'true';
    }
    async function load(id) {
        const serial = ++loadSerial;
        form.dataset.ready = 'false';
        document.getElementById('saveSettingsButton').disabled = true;
        try {
            creating = false;
            const result = await api('/admin/api/branch-settings?branch_id=' + encodeURIComponent(id));
            if (serial !== loadSerial) return;
            fill(result.settings);
            document.getElementById('saveSettingsButton').disabled = false;
        } catch (error) { if (serial === loadSerial) showAlert(error.message, 'error'); }
    }
    async function refreshBranches(selectedId) {
        if (!selector) return;
        const result = await api('/admin/api/branches');
        selector.replaceChildren();
        (result.branches || []).forEach(value => {
            const option = element('option', '', value.name);
            option.value = String(value.id);
            selector.append(option);
        });
        if (selectedId) selector.value = String(selectedId);
    }
    function startNew() {
        loadSerial += 1;
        creating = true;
        if (selector) selector.value = '';
        fill({ name: '', timezone: 'Europe/Moscow', enabled: false, buttons: [] });
        addRow();
        document.getElementById('saveSettingsButton').disabled = false;
        branchId.focus();
    }
    document.getElementById('addSettingsButton').addEventListener('click', () => addRow());
    document.getElementById('newBranchBtn')?.addEventListener('click', startNew);
    selector?.addEventListener('change', () => load(selector.value));
    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (form.dataset.ready !== 'true') { showAlert('Сначала загрузите настройки филиала', 'info'); return; }
        const save = document.getElementById('saveSettingsButton');
        save.disabled = true;
        try {
            const buttons = Array.from(rows.children).map(row => ({
                type: row.querySelector('[data-field="type"]').value,
                label: row.querySelector('[data-field="label"]').value,
                phone: row.querySelector('[data-field="phone"]').value
            }));
            const result = await post('/admin/api/branch-settings', {
                branch_id: Number(branchId.value), create: creating,
                name: document.getElementById('settingsName').value,
                timezone: document.getElementById('settingsTimezone').value,
                enabled: document.getElementById('settingsEnabled').checked,
                logo: document.getElementById('settingsLogo').value, buttons
            });
            creating = false;
            fill(result.settings);
            await refreshBranches(result.settings.branch_id);
            showAlert('Настройки филиала сохранены', 'success');
        } catch (error) { showAlert(error.message, 'error'); }
        finally { save.disabled = false; }
    });
    try {
        if (selector) {
            await refreshBranches();
            if (selector.value) await load(selector.value); else startNew();
        } else {
            await load(window.DEFAULT_BRANCH_ID);
        }
    } catch (error) { showAlert(error.message, 'error'); }
}

function initialize() {
    document.addEventListener('click', event => {
        const nav = document.getElementById('navItems');
        const burger = document.querySelector('.burger-menu');
        if (nav && burger && !nav.contains(event.target) && !burger.contains(event.target)) nav.classList.remove('active');
    });
    if (document.getElementById('usersTableBody')) {
        loadBranches();
        loadUsers();
        ['searchInput', 'filterBranch', 'filterStatus'].forEach(id => {
            document.getElementById(id).addEventListener(id === 'searchInput' ? 'input' : 'change', filterUsers);
        });
        document.getElementById('exportCsvBtn').addEventListener('click', () => { window.location.href = '/admin/api/export-csv'; });
    }
    const accessForm = document.getElementById('accessForm');
    if (accessForm) {
        document.querySelectorAll('input[name="accessType"]').forEach(radio => radio.addEventListener('change', () => {
            document.getElementById('daysInputContainer').style.display = radio.value === 'temporary' && radio.checked ? 'block' : 'none';
        }));
        accessForm.addEventListener('submit', async event => {
            event.preventDefault();
            const temporary = document.querySelector('input[name="accessType"]:checked').value === 'temporary';
            const days = temporary ? Number(document.getElementById('daysInput').value) : null;
            if (temporary && (!Number.isInteger(days) || days < 1 || days > 365)) { showAlert('Введите срок от 1 до 365 дней', 'error'); return; }
            try {
                await post('/admin/api/set-expires', { token: document.getElementById('tokenField').value, days });
                closeModal();
                await loadUsers();
                showAlert('Срок доступа установлен', 'success');
            } catch (error) { showAlert(error.message, 'error'); }
        });
    }
    const createForm = document.getElementById('createLinkForm');
    if (createForm) {
        loadBranches();
        const expiry = document.getElementById('hasExpiry');
        const expiryDays = document.getElementById('expiresDays');
        const send = document.getElementById('sendLink');
        const phone = document.getElementById('phone');
        const syncInputs = () => {
            expiryDays.disabled = !expiry.checked;
            expiryDays.style.display = expiry.checked ? 'block' : 'none';
            document.getElementById('permanentText').classList.toggle('hidden', expiry.checked);
            phone.required = send.checked;
            document.getElementById('phoneLabel').textContent = 'Номер телефона' + (send.checked ? ' *' : '');
        };
        expiry.addEventListener('change', syncInputs);
        send.addEventListener('change', syncInputs);
        syncInputs();
        createForm.addEventListener('submit', async event => {
            event.preventDefault();
            const submit = createForm.querySelector('button[type="submit"]');
            submit.disabled = true;
            try {
                const data = await post('/admin/api/create-link', {
                    user_name: document.getElementById('userName').value,
                    phone: phone.value,
                    branch_id: document.getElementById('branchId') ? Number(document.getElementById('branchId').value) : window.DEFAULT_BRANCH_ID,
                    send_link: send.checked,
                    expires_days: expiry.checked ? Number(expiryDays.value) : null
                });
                document.getElementById('generatedLink').value = fullLink(data.link);
                document.getElementById('linkResult').style.display = 'block';
                showAlert('Пользователь добавлен, ссылка создана', 'success');
                notifyDelivery(data.delivery);
                createForm.reset();
                syncInputs();
            } catch (error) { showAlert(error.message, 'error'); }
            finally { submit.disabled = false; }
        });
    }
    const createTemp = document.getElementById('createTempBtn');
    if (createTemp) {
        loadBranches();
        const send = document.getElementById('sendLinkCheckbox');
        const phone = document.getElementById('tempPhone');
        send.addEventListener('change', () => { phone.style.display = send.checked ? 'block' : 'none'; phone.required = send.checked; });
        createTemp.addEventListener('click', async () => {
            createTemp.disabled = true;
            try {
                const data = await post('/admin/api/create-temp-link', {
                    branch_id: document.getElementById('tempBranch') ? Number(document.getElementById('tempBranch').value) : window.DEFAULT_BRANCH_ID,
                    phone: send.checked ? phone.value : null, send_link: send.checked
                });
                const link = fullLink(data.link);
                await copyToClipboard(link, false);
                showLinkModal('Пропуск создан', data.token, link, data.delivery);
            } catch (error) { showAlert(error.message, 'error'); }
            finally { createTemp.disabled = false; }
        });
    }
    const passwordForm = document.getElementById('changePasswordForm');
    if (passwordForm) {
        document.getElementById('newPassword').addEventListener('input', checkPasswordStrength);
        document.getElementById('confirmPassword').addEventListener('input', checkPasswordMatch);
        passwordForm.addEventListener('submit', async event => {
            event.preventDefault();
            const result = document.getElementById('resultMessage');
            try {
                await post('/admin/api/change-password', {
                    old_password: document.getElementById('oldPassword').value,
                    new_password: document.getElementById('newPassword').value
                });
                result.className = 'result-message success';
                result.textContent = 'Пароль изменен. Другие сеансы завершены.';
                passwordForm.reset();
                checkPasswordStrength();
            } catch (error) { result.className = 'result-message error'; result.textContent = error.message; }
        });
    }
    if (document.getElementById('logsList')) {
        loadLogs();
        ['logType', 'dateFrom', 'dateTo'].forEach(id => document.getElementById(id).addEventListener('change', loadLogs));
        document.getElementById('downloadLogsZipBtn')?.addEventListener('click', () => { window.location.href = '/admin/api/logs-zip'; });
    }
    initializeBranchSettings();
}
initialize();
