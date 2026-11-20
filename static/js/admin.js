// Tenant authorization lives on the server. This UI never treats hidden controls as permissions.
'use strict';

let usersCache = [];
let branchesCache = [];
let sortState = { key: 'pass_changed_at', asc: false };

function element(tag, className = '', text = null) {
    const value = document.createElement(tag);
    if (className) value.className = className;
    if (text !== null) value.textContent = String(text);
    return value;
}

function showAlert(message, type = 'info') {
    const banner = element('div', 'alert alert-' + type + ' admin-toast', message);
    banner.setAttribute('role',type==='error'?'alert':'status');
    document.querySelectorAll('.admin-toast').forEach(previous=>previous.remove());
    document.body.append(banner);
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
        window.location.assign(location.pathname.startsWith('/office/')?'/office/login':'/admin/login');
        throw new Error('Войдите в систему заново');
    }
    if (!response.ok) {
        const error = new Error(data.message || 'Не удалось выполнить действие');
        error.status = response.status; error.data = data;
        error.retry_after = Number(data.retry_after || response.headers.get('Retry-After')) || 0;
        throw error;
    }
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
    delete: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M9 3h6M4 6h16M6 6l1 14h10l1-14M10 10v8M14 10v8" stroke="#272b2e" stroke-width="2"/></svg>',
    admin: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 3 7v5c0 5 9 9 9 9s9-4 9-9V7l-9-4Z"/><path d="M12 8v7m-3-4h6"/></svg>'
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
    const trigger=document.activeElement;
    const modal = element('div', 'modal');
    modal.dataset.dynamic='true';
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
    setTimeout(()=>modal.querySelector('input:not([readonly]),select,button')?.focus(),0);
    const close=()=>{modal.remove();if(trigger?.isConnected)trigger.focus();};
    modal.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();close();}});
    return { modal, body, actions, close };
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
    if (user.admin_role_active || user.admin_role?.is_admin || user.admin_role?.is_super || !user.access_expires) return { category: 'Постоянный', text: 'Постоянный', className: 'btn-pass-permanent' };
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

function passChangedAge(value,current=Date.now()) {
    const changed=Date.parse(value);
    if(!Number.isFinite(changed))return '—';
    const minutes=Math.max(1,Math.floor((current-changed)/60000));
    return minutes<60?minutes+'м':minutes<1440?Math.floor(minutes/60)+'ч':Math.floor(minutes/1440)+'д';
}

function refreshPassAges(){
    document.querySelectorAll('[data-pass-changed-at]').forEach(node=>{node.textContent=passChangedAge(node.dataset.passChangedAt);});
}

function displayUsers(users) {
    const tbody = document.getElementById('usersTableBody');
    tbody.replaceChildren();
    const counter=document.getElementById('clientCount');if(counter)counter.textContent=`${users.length} из ${usersCache.length}`;
    const selectedId=document.getElementById('filterBranch')?.value||window.DEFAULT_BRANCH_ID;
    const selectedBranch=branchesCache.find(branch=>String(branch.id)===String(selectedId));
    const contextUsers=selectedId?usersCache.filter(user=>String(user.branch_id)===String(selectedId)):usersCache;
    const hideCrm=selectedBranch?.yclients_enabled===false||(!selectedBranch&&contextUsers.length>0&&contextUsers.every(user=>user.yclients_enabled===false));
    const crmHeader=tbody.closest('table').querySelector('th[onclick*="has_yclients"]');if(crmHeader)crmHeader.hidden=hideCrm;
    const crmSort=document.querySelector('#mobileClientSort option[value=has_yclients]');if(crmSort)crmSort.hidden=hideCrm;
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
        const nameCell=cell('Имя');nameCell.append(element('span','',user.user_name||'—'));
        const role=user.admin_role||{};
        if(role.is_admin||role.is_super){const badge=element('span','client-role-badge',role.is_super?'Администрация сервиса GateBot':role.last_admin?'Администратор · единственный':'Администратор');nameCell.append(element('br'),badge);}
        cell('Телефон', user.phone || '—');
        cell('Email', user.email || '—');
        cell('Филиал', user.branch_name || '—').classList.add('client-branch');
        const passCell=cell('Пропуск');passCell.append(button(pass.text, () => onPassButtonClick(user.token, !!user.gate_access), 'btn-pass ' + pass.className));
        const branchReason={branch_disabled:'Доступ филиала выключен',branch_missing:'Филиал не настроен'}[user.branch_access_reason];
        if(branchReason)passCell.append(element('small','client-branch-access-hint',branchReason));
        const changedCell=cell('Изменён');
        if(user.pass_changed_at){
            const changed=element('time','',passChangedAge(user.pass_changed_at));
            changed.dateTime=user.pass_changed_at;changed.dataset.passChangedAt=user.pass_changed_at;
            changed.title='Пропуск изменён: '+new Date(user.pass_changed_at).toLocaleString('ru-RU');
            changedCell.append(changed);
        }else changedCell.textContent='—';
        cell('Боты', [user.has_telegram ? 'TG' : '', user.has_max ? 'MAX' : ''].filter(Boolean).join(' · ') || '—');
        cell('YCLIENTS', user.has_yclients ? '✓' : '').hidden=hideCrm;
        cell('Уведомления').append(clientNotificationStatusButton(user));
        const actions = cell('Действия');
        actions.style.whiteSpace = 'nowrap';
        actions.style.textAlign = 'right';
        actions.append(iconButton('Копировать ссылку', 'copy', () => onCopyUserLink(user.token)),iconButton('Редактировать', 'edit', () => openEditUserModal(user.token)));
        if(!role.is_admin&&!role.is_super)actions.append(iconButton('Назначить администратором', 'admin', () => inviteAdministrator(user)));
        const remove=iconButton('Удалить', 'delete', () => onDeleteUser(user.token));
        if(role.can_delete===false){remove.disabled=true;remove.title=role.reason||'Удаление недоступно';}
        actions.append(remove);
        tbody.append(row);
    });
}

function filterUsers() {
    const search = (document.getElementById('searchInput')?.value || '').toLowerCase();
    const branchId = document.getElementById('filterBranch')?.value || '';
    const status = document.getElementById('filterStatus')?.value || '';
    let users = usersCache.filter(user => {
        return (!search || String(user.user_name || '').toLowerCase().includes(search) || String(user.phone || '').includes(search) || String(user.email || '').toLowerCase().includes(search)) &&
            (!branchId || String(user.branch_id) === branchId) && (!status || passInfo(user).category === status);
    });
    if (sortState.key) {
        users = [...users].sort((a, b) => {
            if(sortState.key==='pass_changed_at'){
                const left=Date.parse(a.pass_changed_at),right=Date.parse(b.pass_changed_at);
                if(!Number.isFinite(left))return Number.isFinite(right)?1:0;
                if(!Number.isFinite(right))return -1;
                return (left-right)*(sortState.asc?1:-1);
            }
            const getValue = u => sortState.key === 'pass_label' ? passInfo(u).text : sortState.key === 'notification_channels' ? Object.entries(u.notification_status?.effective_channels||{}).filter(([,active])=>active).map(([key])=>key).join(',') : (u[sortState.key] ?? '');
            return String(getValue(a)).localeCompare(String(getValue(b)), 'ru', { numeric: true }) * (sortState.asc ? 1 : -1);
        });
    }
    displayUsers(users);
}

function sortUsersBy(key) {
    sortState = { key, asc: sortState.key === key ? !sortState.asc : key!=='pass_changed_at' };
    const mobile=document.getElementById('mobileClientSort'),direction=document.getElementById('mobileSortDirection');
    if(mobile&&direction){mobile.value=key;direction.textContent=sortState.asc?'↑':'↓';direction.title=sortState.asc?'По возрастанию':'По убыванию';direction.setAttribute('aria-label',direction.title+' · изменить');}
    filterUsers();
    document.querySelectorAll('.users-table th[onclick]').forEach(th=>th.setAttribute('aria-sort',th.getAttribute('onclick').includes("'"+key+"'")?(sortState.asc?'ascending':'descending'):'none'));
}

function inviteAdministrator(user) {
    const view=createModal('Пригласить администратора');
    view.body.append(element('p','',`${user.user_name || 'Клиент'} получит приглашение управлять филиалом. Ссылка действует один час. Роль появится после регистрации и проверки телефона.`));
    const group=element('div','form-group'),caption=element('label','','Канал приглашения'),select=element('select');
    for(const [value,title,available] of [['sms','SMS',!!user.phone],['chatpush','ChatPush',!!user.phone],['email','Email',!!user.email],['telegram','Telegram',!!user.has_telegram],['max','MAX',!!user.has_max]]){
        if(value==='chatpush'&&user.yclients_enabled===false)continue;
        const option=element('option','',title+(available?'':' — не подключён'));option.value=value;option.disabled=!available;select.append(option);
    }
    caption.append(select);group.append(caption);view.body.append(group);
    const send=button('Создать приглашение',async()=>{
        send.disabled=true;
        try{const result=await post('/admin/invitations/new',{client_token:user.token,branch_id:user.branch_id,channel:select.value});view.close();
            const resultView=createModal('Приглашение создано'),box=element('div','link-box'),input=element('input');input.value=result.link;input.readOnly=true;box.append(input,iconButton('Копировать приглашение','copy',()=>copyToClipboard(result.link)));resultView.body.append(box,element('p','form-hint','Ссылка действует один час. Регистрация требует кода из SMS.'));
            if(result.delivery?.message)resultView.body.append(element('p','form-hint',result.delivery.message));
            resultView.actions.append(button('Закрыть',resultView.close));
        }catch(error){showAlert(error.message,'error');}finally{send.disabled=false;}
    });view.actions.append(button('Отмена',view.close),send);
}

async function loadBranches() {
    try {
        const data = await api('/admin/api/branches');
        const branches = data.branches || [];
        branchesCache=branches;
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
        if(document.getElementById('usersTableBody'))filterUsers();
        window.dispatchEvent(new Event('gatebot:branches-loaded'));
    } catch (error) { showAlert(error.message, 'error'); }
}

function openEditUserModal(token) {
    const user = usersCache.find(value => value.token === token);
    if (!user) return;
    const view = createModal('Редактировать пользователя');
    const roleInfo=element('div','client-role-details');roleInfo.hidden=true;roleInfo.dataset.roleInfo='true';view.body.append(roleInfo);
    function showRole(role){
        if(!view.modal.isConnected)return;roleInfo.replaceChildren();roleInfo.hidden=!role.is_admin&&!role.is_super;if(roleInfo.hidden)return;
        roleInfo.append(element('strong','',role.is_super?'Администрация сервиса GateBot':'Администратор филиала'),element('p','form-hint',role.reason||'Права администратора действуют в веб-кабинете и ботах. Можно отозвать роль, сохранив карточку клиента.'));
        const revoke=button('Отозвать права администратора',()=>confirmRevokeClientAdmin(user,view),'btn-action btn-deny');revoke.disabled=role.can_revoke!==true;roleInfo.append(revoke);
    }
    if(user.admin_role){showRole({...user.admin_role,can_revoke:false});api('/admin/api/client-role?token='+encodeURIComponent(token)).then(result=>{user.admin_role=result.admin_role;showRole(result.admin_role);}).catch(error=>{if(view.modal.isConnected){roleInfo.hidden=false;roleInfo.append(element('p','form-hint',error.message));}});}
    const name = formField(view.body, 'Имя', user.user_name);
    const phone = formField(view.body, 'Телефон', user.phone, 'tel');
    const email = formField(view.body, 'Email', user.email, 'email');
    email.maxLength = 254;
    email.autocomplete = 'email';
    email.placeholder = 'Необязательно';
    const crm = user.yclients_enabled===false?null:formField(view.body, 'YCLIENTS ID', user.id_ycl);
    let lookupBusy = false;
    if (crm) {
        const label = crm.parentElement, group = label.parentElement;
        crm.id = 'clientYclientsId'; label.htmlFor = crm.id;
        const row = element('div', 'client-yc-id-control'), hint = element('p', 'form-hint');
        hint.dataset.yclientsIdHint = 'true'; hint.setAttribute('role', 'status');
        const lookup = button('', async () => {
            if (lookupBusy || changedPhone()) return;
            lookupBusy = true; updateLookup(); phone.disabled = crm.disabled = save.disabled = true;
            hint.textContent = 'Ищем клиента по сохранённому номеру телефона…';
            try {
                const response = await post('/admin/api/yclients/client-id', {token});
                const result = response.result || {};
                if (['updated', 'unchanged'].includes(result.status)) {
                    user.id_ycl = result.id_ycl;
                    const current = usersCache.find(item => item.token === token);
                    if (current) current.id_ycl = result.id_ycl;
                    if (view.modal.isConnected) crm.value = result.id_ycl ?? '';
                    if (document.getElementById('usersTableBody')) filterUsers();
                }
                if (view.modal.isConnected) hint.textContent = result.message || 'Проверка завершена. ID не изменён.';
            } catch (error) {
                if (view.modal.isConnected) hint.textContent = error.message;
            } finally {
                lookupBusy = false;
                if (view.modal.isConnected) { phone.disabled = crm.disabled = save.disabled = false; updateLookup(); }
            }
        }, 'btn-action client-yc-id-refresh');
        lookup.title = 'Обновить ID из YCLIENTS'; lookup.setAttribute('aria-label', lookup.title);
        lookup.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 6a8 8 0 0 1 13.2 3M17.9 18a8 8 0 0 1-13.2-3"/></svg>';
        row.append(crm, lookup); group.append(row, hint);
        const normalizedPhone = value => window.GateBotPhone?.normalize(value) || value.trim();
        const savedPhone = normalizedPhone(user.phone || '');
        function changedPhone() { return normalizedPhone(phone.value) !== savedPhone; }
        function updateLookup() {
            lookup.disabled = lookupBusy || changedPhone();
            if (changedPhone()) hint.textContent = 'Сначала сохраните новый номер телефона, затем обновите ID.';
        }
        phone.addEventListener('input', () => {
            if (!lookupBusy && !changedPhone()) hint.textContent = '';
            updateLookup();
        });
        updateLookup();
    }
    formField(view.body, 'Telegram — подтвержденная привязка', user.tg_id || 'Не привязан', 'text', true);
    formField(view.body, 'MAX — подтвержденная привязка', user.max_id || 'Не привязан', 'text', true);
    view.body.append(element('p', 'form-hint', 'После смены телефона пользователь заново привязывает мессенджер по персональной ссылке или своему контакту.'));
    const save = button('Сохранить', async () => {
        if (lookupBusy) return;
        if (!email.reportValidity()) return;
        save.disabled = true;
        try {
            await post('/admin/api/update-user', { token, user_name: name.value, phone: phone.value, email: email.value, ...(crm?{id_ycl:crm.value}:{}) });
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

let accessModalTrigger=null;
function showAccessModal(token) {
    accessModalTrigger=document.activeElement;
    document.getElementById('tokenField').value = token;
    document.getElementById('accessModal').style.display = 'block';
    document.querySelector('#accessModal input[name="accessType"]:checked')?.focus();
}

function closeModal() {
    document.getElementById('accessModal').style.display = 'none';
    if(accessModalTrigger?.isConnected)accessModalTrigger.focus();
}

async function onDeleteUser(token) {
    const user=usersCache.find(item=>item.token===token);let role=user?.admin_role||{};
    try{if(user?.admin_role)role=(await api('/admin/api/client-role?token='+encodeURIComponent(token))).admin_role;}catch(error){showAlert(error.message,'error');return;}
    if(role.can_delete===false){showAlert(role.reason||'Удаление недоступно','error');return;}
    const view = createModal('Удалить пользователя?');
    view.body.append(element('p', '', role.is_admin?'Это администратор филиала. Удаление клиента также отзовёт его права администратора, доступ к кабинету и административным функциям ботов. Пропуск и клиентские привязки будут удалены.':'Пропуск и привязки мессенджеров будут удалены.'));
    const confirm = button('Удалить', async () => {
        confirm.disabled = true;
        try {
            await post('/admin/api/delete-user', { token,confirm_admin:role.is_admin===true });
            view.close();
            await loadUsers();
            showAlert('Пользователь удален', 'success');
        } catch (error) { showAlert(error.message, 'error'); }
        finally { confirm.disabled = false; }
    }, 'btn-action btn-deny');
    view.actions.append(button('Отмена', view.close), confirm);
}

function confirmRevokeClientAdmin(user,editor){
    const view=createModal('Отозвать права администратора?');view.body.append(element('p','','Карточка клиента сохранится. Доступ к административным функциям будет закрыт; пропуск по роли администратора перестанет действовать. Независимый ручной или CRM-пропуск сохраняется.'));
    const confirm=button('Отозвать права',async()=>{confirm.disabled=true;try{await post('/admin/api/client-role/revoke',{token:user.token});view.close();editor.close();await loadUsers();showAlert('Права администратора отозваны. Клиент сохранён.','success');}catch(error){showAlert(error.message,'error');}finally{confirm.disabled=false;}},'btn-action btn-deny');view.actions.append(button('Отмена',view.close),confirm);
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

async function openSendLinkModal(token) {
    let user = usersCache.find(value => value.token === token);
    if(!user){
        try{user=(await api('/admin/api/users')).users.find(value=>value.token===token);}catch(error){showAlert(error.message,'error');return;}
    }
    if(!user){showAlert('Клиент больше недоступен','error');return;}
    const view = createModal('Отправить ссылку');
    view.body.append(element('p', '', 'Ссылка будет передана подключенному сервису уведомлений.'));
    const phone = formField(view.body, 'Номер телефона', user.phone || '', 'tel');
    const channelLabel=element('label','','Канал отправки'),channel=element('select');
    for(const [value,title,available] of [['messengers','Мессенджеры',user.yclients_enabled!==false],['sms','СМС — расходуется лимит филиала',user.sms_notifications_enabled===true],['email','Email',Boolean(user.email)]]){
        if(!available)continue;const option=element('option','',title);option.value=value;channel.append(option);
    }
    channelLabel.append(channel);view.body.append(channelLabel);
    if(!channel.options.length)view.body.append(element('p','form-hint','Укажите Email в карточке клиента или включите SMS-уведомления филиала.'));
    const confirm = button('Отправить', async () => {
        confirm.disabled = true;
        try {
            const result = await post('/admin/api/send-link', { token, phone: phone.value,channel:channel.value });
            view.close();
            showAlert(result.message || 'Сообщение принято сервисом отправки', 'success');
            if (document.getElementById('usersTableBody')) await loadUsers();
        } catch (error) { showAlert(error.message, 'error'); }
        finally { confirm.disabled = false; }
    });
    confirm.disabled=!channel.options.length;
    view.actions.append(button('Отмена', view.close), confirm);
}

function notifyDelivery(delivery) {
    if (delivery?.status === 'sent') showAlert(delivery.message, 'success');
    else if (delivery && delivery.status !== 'not_requested') showAlert('Ссылка создана, но не отправлена: ' + delivery.message, 'info');
}

function setupPassDelivery(send,channel,hint,branchSelector,emailInput) {
    if(!send||!channel)return;
    function sync(){
        const branch=branchesCache.find(value=>String(value.id)===String(branchSelector?.value||window.DEFAULT_BRANCH_ID));
        const selected=channel.value, choices=[];
        if(branch?.yclients_enabled!==false&&branch)choices.push(['messengers','Мессенджеры']);
        if(branch?.sms_notifications_enabled===true)choices.push(['sms','СМС — расходуется лимит филиала']);
        if(branch)choices.push(['email','Email']);
        channel.replaceChildren();
        for(const [value,title] of choices){const option=element('option','',title);option.value=value;channel.append(option);}
        if(choices.some(([value])=>value===selected))channel.value=selected;
        send.disabled=!choices.length;if(send.disabled)send.checked=false;
        channel.disabled=!send.checked||!choices.length;
        if(emailInput)emailInput.required=send.checked&&channel.value==='email';
        if(hint)hint.textContent=channel.value==='email'?'Укажите Email получателя. Письмо отправляется с адреса сервиса.':channel.value==='sms'?'SMS расходуют лимит тарифа и пакетов. Длинная ссылка может занимать несколько SMS-частей.':'Ссылка отправляется через подключённый сервис мессенджеров филиала.';
    }
    send.addEventListener('change',sync);channel.addEventListener('change',sync);branchSelector?.addEventListener('change',sync);
    window.addEventListener('gatebot:branches-loaded',sync);sync();return sync;
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
    const crmMode = document.getElementById('settingsYclients');
    const logoValue = document.getElementById('settingsLogo');
    const logoUpload = document.getElementById('settingsLogoUpload');
    const logoPreview = document.getElementById('settingsLogoPreview');
    const logoRemove = document.getElementById('settingsLogoRemove');
    const logoStatus = document.getElementById('settingsLogoStatus');
    let creating = false;
    let internalId = false;
    let loadSerial = 0;
    form.dataset.ready = 'false';
    function showBranchMode() {
        const crm=crmMode.value==='true';
        crmMode.disabled=!window.IS_SUPER||(!creating&&internalId);
        branchId.required=creating&&crm;
        branchId.readOnly=!creating||!crm;
        branchId.closest('.form-group').hidden=(creating&&!crm)||(!window.IS_SUPER&&!crm);
        if(creating&&!crm)branchId.value='';
        if(creating&&!crm)for(const row of Array.from(rows.children))if(!row.querySelector('[data-field="phone"]').value)row.remove();
        document.getElementById('settingsBranchIdLabel').textContent=internalId?'Внутренний ID филиала':crm?'ID филиала в YCLIENTS':'ID филиала';
        document.getElementById('settingsYclientsHint').textContent=creating&&!crm?'Внутренний ID назначится автоматически при создании.':internalId?'Филиал работает самостоятельно. Внутренний ID сохраняется на весь срок работы.':'Для самостоятельного филиала интеграции YCLIENTS и ChatPush недоступны.';
    }
    crmMode.addEventListener('change',showBranchMode);
    function showLogo(url) {
        logoValue.value = url || '';
        logoPreview.hidden = !url;
        if (url) logoPreview.src = url; else logoPreview.removeAttribute('src');
        logoRemove.hidden = !url;
    }
    async function changeLogo(file) {
        if (creating || form.dataset.ready !== 'true') return;
        if (file && file.size > 5 * 1024 * 1024) {
            logoStatus.textContent = 'Выберите изображение размером до 5 МБ';
            logoUpload.value = '';
            return;
        }
        const selectedBranch = branchId.value;
        const save = document.getElementById('saveSettingsButton');
        const add = document.getElementById('newBranchBtn');
        logoUpload.disabled = logoRemove.disabled = save.disabled = true;
        if (selector) selector.disabled = true;
        if (add) add.disabled = true;
        logoStatus.textContent = file ? 'Загружаем изображение…' : 'Удаляем логотип…';
        try {
            const body = new FormData();
            if (file) body.append('image', file);
            const response = await fetch('/admin/api/branches/' + encodeURIComponent(selectedBranch) + '/logo', {
                method: file ? 'POST' : 'DELETE', credentials: 'same-origin',
                headers: {'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content},
                ...(file ? {body} : {})
            });
            let result;
            try { result = await response.json(); } catch { throw new Error('Не удалось обработать изображение'); }
            if (!response.ok) throw new Error(result.message || 'Не удалось сохранить логотип');
            if (branchId.value === selectedBranch) showLogo(result.logo);
            logoStatus.textContent = file ? 'Логотип сохранён' : 'Логотип удалён';
        } catch (error) { logoStatus.textContent = error.message; }
        finally {
            logoUpload.value = '';
            logoUpload.disabled = logoRemove.disabled = save.disabled = false;
            if (selector) selector.disabled = false;
            if (add) add.disabled = false;
        }
    }
    logoUpload.addEventListener('change', () => { if (logoUpload.files[0]) changeLogo(logoUpload.files[0]); });
    logoRemove.addEventListener('click', () => changeLogo(null));
    function addRow(value = { type: 'gate', label: 'Въезд', phone: '' }) {
        if (rows.children.length >= 12) { showAlert('Можно добавить до 12 кнопок', 'info'); return; }
        const row = element('tr');
        if(value.stable_id)row.dataset.stableId=value.stable_id;
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
        type.addEventListener('change',()=>{delete row.dataset.stableId;});
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
        internalId=value.id_origin==='internal';
        crmMode.value=value.yclients_enabled===false?'false':'true';
        showBranchMode();
        document.getElementById('settingsName').value = value.name || '';
        document.getElementById('settingsTimezone').value = value.timezone || 'Europe/Moscow';
        showLogo(value.logo || '');
        logoUpload.value = '';
        logoUpload.disabled = creating;
        logoStatus.textContent = creating ? 'Сначала сохраните новый филиал, затем загрузите логотип.' : '';
        document.getElementById('settingsEnabled').checked = value.enabled === true;
        document.getElementById('settingsSmsNotifications').checked = value.sms_notifications_enabled === true;
        rows.replaceChildren();
        (value.buttons || []).forEach(addRow);
        rows.querySelectorAll('input,select,button').forEach(control=>control.disabled=false);
        document.getElementById('addSettingsButton').disabled=false;
        document.getElementById('saveSettingsButton').textContent = creating ? 'Создать филиал' : 'Сохранить настройки';
        form.dataset.ready = 'true';
        document.dispatchEvent(new CustomEvent('gatebot:branch-settings-loaded',{detail:{branchId:value.branch_id,buttons:value.buttons||[]}}));
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
                phone: row.querySelector('[data-field="phone"]').value,
                ...(row.dataset.stableId?{stable_id:row.dataset.stableId}:{})
            }));
            const result = await post('/admin/api/branch-settings', {
                ...(!(creating&&crmMode.value==='false')?{branch_id:Number(branchId.value)}:{}), create: creating,
                yclients_enabled:crmMode.value==='true',
                name: document.getElementById('settingsName').value,
                timezone: document.getElementById('settingsTimezone').value,
                enabled: document.getElementById('settingsEnabled').checked,
                sms_notifications_enabled:document.getElementById('settingsSmsNotifications').checked,
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
        window.setInterval(refreshPassAges,60000);
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
        const syncDelivery=setupPassDelivery(send,document.getElementById('createDeliveryChannel'),document.getElementById('createDeliveryHint'),document.getElementById('branchId'),document.getElementById('clientEmail'));
        const syncInputs = () => {
            expiryDays.disabled = !expiry.checked;
            expiryDays.style.display = expiry.checked ? 'block' : 'none';
            document.getElementById('permanentText').classList.toggle('hidden', expiry.checked);
            phone.required = true;
            document.getElementById('phoneLabel').textContent = 'Номер телефона *';
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
                    email: document.getElementById('clientEmail').value,
                    branch_id: document.getElementById('branchId') ? Number(document.getElementById('branchId').value) : window.DEFAULT_BRANCH_ID,
                    send_link: send.checked,
                    channel:document.getElementById('createDeliveryChannel').value||'auto',
                    expires_days: expiry.checked ? Number(expiryDays.value) : null
                });
                document.getElementById('generatedLink').value = fullLink(data.link);
                document.getElementById('linkResult').style.display = 'block';
                showAlert('Пользователь добавлен, ссылка создана', 'success');
                notifyDelivery(data.delivery);
                createForm.reset();
                syncInputs();
                syncDelivery?.();
            } catch (error) { showAlert(error.message, 'error'); }
            finally { submit.disabled = false; }
        });
    }
    const createTemp = document.getElementById('createTempBtn');
    if (createTemp) {
        loadBranches();
        const send = document.getElementById('sendLinkCheckbox');
        const phone = document.getElementById('tempPhone');
        const email=document.getElementById('tempEmail');
        setupPassDelivery(send,document.getElementById('tempDeliveryChannel'),document.getElementById('tempDeliveryHint'),document.getElementById('tempBranch'),email);
        phone.required = true;
        createTemp.addEventListener('click', async () => {
            if(!phone.value.trim() || !phone.checkValidity()){phone.reportValidity();return;}
            if(!email.checkValidity()){email.reportValidity();return;}
            createTemp.disabled = true;
            try {
                const data = await post('/admin/api/create-temp-link', {
                    branch_id: document.getElementById('tempBranch') ? Number(document.getElementById('tempBranch').value) : window.DEFAULT_BRANCH_ID,
                    phone: phone.value,email:email.value,send_link: send.checked,channel:document.getElementById('tempDeliveryChannel').value||'auto'
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
