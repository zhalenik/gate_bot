'use strict';
(() => {
  const root = document.querySelector('[data-billing-page]'); if (!root) return;
  const $ = id => document.getElementById(id), superUser = root.dataset.super === 'true', view = root.dataset.view;
  const el = (tag, text = '', cls = '') => {const n = document.createElement(tag); n.textContent = text; n.className = cls; return n;};
  const money = n => (Number(n || 0) / 100).toLocaleString('ru-RU') + ' ₽';
  const date = value => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('ru-RU', {day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'}) : 'Без срока';
  const number = value => value == null ? 'Без лимита' : Number(value).toLocaleString('ru-RU');
  const duration = plan => plan?.duration_value ? `${plan.duration_value} ${plan.duration_unit === 'months' ? 'мес.' : 'дн.'}` : 'Текущий период';
  const labels = {active:'Действует',trial:'Пробный',legacy:'Сохранённый доступ',expired:'Срок истёк',suspended:'Приостановлен',cancelled:'Отключён',pending:'Ожидает начала',gift:'Подарок',paid:'Оплачен',awaiting_payment:'Ожидает оплаты',command_sent:'Команда передана',network_accepted:'Принято сетью',partial:'Частично отправлено',unknown:'Исход неизвестен',failed:'Ошибка',queued:'В очереди',sending:'Отправляется',simulated:'Имитация',calls:'Звонки',sms:'SMS'};
  const operations = {tariff_order_created:'Заказ тарифа',package_order_created:'Заказ пакета',tariff_activated:'Подключение тарифа',package_activated:'Подключение пакета',subscription_cancelled:'Отключение тарифа',subscription_renewal_enabled:'Автопродление разрешено',subscription_renewal_cancelled:'Автопродление отключено',scheduled_change_cancelled:'Запланированная смена отменена',offer_accepted:'Принята оферта',renewal_consent_accepted:'Согласие на автосписания',trial_activated:'Пробный тариф',gift_granted:'Подарок тарифа',quota_gift:'Подарок квоты',payment_applied:'Оплата применена',order_created:'Создан заказ',subscription_suspended:'Доступ приостановлен',subscription_resumed:'Доступ возобновлён',billing_timezone_scheduled:'Изменение расчётного часового пояса'};
  labels.test = 'Тестовый'; operations.tariff_test_activated = 'Тестовое подключение без оплаты';
  let catalog = {plans:[],packages:[],offer:null}, branches = [], branchId = '', current = null, packageFilter = 'all', testActivationAvailable = false, testActivationKey = '';
  let detailGeneration = 0, historyGeneration = 0, historyPage = 1, historyKind = 'payments', purchase = null, quoteGeneration = 0, editing = null;
  const busy = new WeakSet();
  function notify(text, error = false) {const n = $('productNotice'); n.hidden = !text; n.textContent = text; n.className = 'alert ' + (error ? 'alert-error' : 'alert-info');}
  async function request(path, data) {
    const form = data instanceof FormData, headers = {'X-CSRF-Token':document.querySelector('meta[name=csrf-token]').content};
    if (data !== undefined && !form) headers['Content-Type'] = 'application/json';
    const response = await fetch(path, {method:data === undefined ? 'GET' : 'POST',credentials:'same-origin',headers,body:data === undefined ? undefined : form ? data : JSON.stringify(data)});
    let result; try {result = await response.json();} catch {throw Error('Не удалось прочитать ответ. Обновите страницу.');}
    if (!response.ok || result.status === 'error') throw Error(result.message || 'Не удалось выполнить действие');
    return result;
  }
  function on(target, event, action) {
    target?.addEventListener(event, async e => {
      e.preventDefault(); if (busy.has(target)) return; busy.add(target);
      const submit = target.tagName === 'FORM' ? target.querySelector('[type=submit]') : null;
      if (submit) submit.disabled = true;
      const error = target.closest('dialog')?.querySelector('[data-dialog-error],#purchaseError');
      if (error) error.hidden = true;
      try {await action(e);} catch (failure) {if (error) {error.textContent = failure.message; error.hidden = false;} else notify(failure.message, true);}
      finally {busy.delete(target); if (submit) submit.disabled = false;}
    });
  }
  function icon(resource) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('viewBox','0 0 24 24'); svg.setAttribute('fill','none'); svg.setAttribute('stroke','currentColor'); svg.setAttribute('stroke-width','1.7'); svg.setAttribute('stroke-linecap','round'); svg.setAttribute('stroke-linejoin','round'); svg.setAttribute('aria-hidden','true');
    const path = document.createElementNS(svg.namespaceURI,'path'); path.setAttribute('d',resource === 'sms' ? 'M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-5 3v-3H3V6a2 2 0 0 1 2-2ZM7 9h10M7 13h7' : 'M7 3 3 5c-1 8 8 17 16 16l2-4-5-3-2 2c-3-1-5-3-6-6l2-2-3-5Z'); svg.append(path); return svg;
  }
  function logo(target, item) {target.replaceChildren(); if (/^\/admin\/billing-assets\/[a-f0-9]{32}\.png$/.test(item?.logo || '')) {const img = el('img'); img.src = item.logo; img.alt = ''; target.append(img);} else target.textContent = (item?.name || 'GB').slice(0,2).toUpperCase();}
  function offerLink(target, offer) {if (!target) return; target.hidden = !offer?.url; if (offer?.url) target.href = offer.url;}
  function button(text, cls, fn) {const n = el('button', text, cls); n.type = 'button'; on(n,'click',fn); return n;}
  function option(select, value, text) {const n = el('option',text); n.value = value; select.append(n);}
  function closeDialog(dialog) {dialog.close(); if (dialog.id === 'purchaseDialog') {purchase = null; quoteGeneration++;}}
  root.querySelectorAll('[data-dismiss]').forEach(n => on(n,'click',() => closeDialog(n.closest('dialog'))));
  root.querySelectorAll('dialog').forEach(n => n.addEventListener('close',() => {if (n.id === 'purchaseDialog') {purchase = null; quoteGeneration++;}}));
  function meter(resource, data) {
    data ||= {quota:null,spent:0,reserved:0,credits:0,remaining:null};
    const box = el('article','','billing-meter'), heading = el('div','','billing-meter-heading'), symbol = el('span','','billing-resource-icon'); symbol.append(icon(resource)); heading.append(symbol,el('span',labels[resource]));
    const value = el('div','','billing-meter-value'); value.append(el('strong',data.remaining == null ? '∞' : number(data.remaining)),el('span',data.quota == null ? 'без ограничения' : `осталось из ${number(data.quota)}`));
    const progress = el('progress'); progress.max = data.quota == null ? 1 : Math.max(1,data.quota); progress.value = data.remaining == null ? 1 : Math.max(0,data.remaining); progress.setAttribute('aria-label',`Остаток ${resource === 'sms' ? 'SMS' : 'звонков'}: ${number(data.remaining)}`);
    const foot = el('div','','billing-meter-foot'); foot.append(el('span',`Израсходовано: ${number(data.spent)}`),el('span',data.reserved ? `В очереди: ${number(data.reserved)}` : data.credits ? `Дополнительно: ${number(data.credits)}` : 'В пределах периода'));
    box.classList.toggle('is-low',data.quota != null && data.remaining <= data.quota * .1); box.append(heading,value,progress,foot); return box;
  }
  function renderOverview() {
    if (!$('billingBranches')) return;
    const search = ($('billingSearch').value || '').toLowerCase(); $('billingBranches').replaceChildren();
    for (const b of branches.filter(b => `${b.name} ${b.plan?.name || ''} ${labels[b.status] || b.status}`.toLowerCase().includes(search))) {
      const tr = el('tr'), first = el('td'); first.append(button(b.name,'billing-row-link',async () => {$('billingBranch').value = b.branch_id; branchId = b.branch_id; historyPage = 1; await detail();})); tr.append(first);
      const inactive = ['expired','cancelled','pending'].includes(b.status);
      for (const value of [inactive ? 'Не подключён' : b.plan?.name || labels[b.source],inactive ? '—' : date(b.ends_at),inactive ? '—' : number(b.calls?.remaining ?? b.remaining),inactive ? '—' : number(b.sms?.remaining),labels[b.status] || b.status]) tr.append(el('td',value)); $('billingBranches').append(tr);
    }
  }
  function renderAccount() {
    if (!current) return;
    const inactive = ['expired','cancelled','pending'].includes(current.status);
    const plan = current.plan || {}; logo($('currentPlanLogo'),plan); $('currentPlanName').textContent = plan.name || labels[current.source] || 'Тариф';
    $('subscriptionStatus').textContent = labels[current.status] || current.status; $('subscriptionStatus').className = 'badge ' + (['expired','cancelled','suspended'].includes(current.status) ? 'warning' : 'success');
    $('subscriptionPeriod').textContent = 'Подписка действительна до: ' + date(current.ends_at);
    $('currentPlanPrice').textContent = current.source === 'gift' ? 'Подарок' : plan.kind === 'trial' ? 'Бесплатно' : money(plan.price_kopecks ?? current.price_kopecks);
    $('currentPlanDuration').textContent = duration(plan); $('subscriptionSummary').replaceChildren(meter('calls',current.calls || current),meter('sms',current.sms));
    $('billingHero').classList.toggle('is-inactive',inactive); $('billingHeroPrice').hidden = inactive;
    $('currentPlanLogo').hidden = inactive;
    $('subscriptionSummary').hidden = $('billingActiveDetails').hidden = inactive; $('subscriptionUnavailable').hidden = !inactive;
    if (inactive) {
      $('currentPlanName').textContent = current.status === 'cancelled' ? 'Тариф отключён' : current.status === 'expired' ? 'Срок тарифа закончился' : 'Тариф ещё не действует';
      $('subscriptionPeriod').textContent = plan.name ? 'Предыдущий тариф: ' + plan.name : '';
      $('subscriptionUnavailable').textContent = 'Для работы сервиса подключите тариф. История платежей и расхода сохранена ниже.';
    } else if (current.source === 'test') {$('currentPlanPrice').textContent = 'Для тестирования'; $('subscriptionPeriod').textContent += ' · Подключён без оплаты';}
    if ($('selectTariff')) $('selectTariff').textContent = inactive ? 'Подключить тариф' : 'Изменить тариф';
    if ($('selectPackage')) $('selectPackage').hidden = inactive;
    $('openCancellation').hidden = inactive;
    if ($('suspendBilling')) $('suspendBilling').hidden = inactive;
    if ($('openTestActivation')) $('openTestActivation').hidden = !testActivationAvailable;
    const renewal = current.renewal || {}; $('renewalStatus').textContent = renewal.enabled ? 'Разрешено' : 'Отключено';
    $('renewalDescription').textContent = plan.kind === 'trial' ? 'Пробный тариф доступен один раз и не продлевается. Выберите платный тариф, чтобы продолжить работу после его окончания.' : renewal.enabled ? `Стоимость следующего периода: ${money(plan.price_kopecks)}. ${renewal.payment_available ? 'Следующее списание: ' + date(renewal.next_charge_at) + '.' : 'Автоплатёж пока не подключён. Согласие на продление сохранено.'}` : 'Автоматических списаний нет. Текущий тариф действует до указанной даты.';
    if ($('renewalAction')) {$('renewalAction').textContent = renewal.enabled ? 'Отключить автопродление' : 'Разрешить автопродление'; $('renewalAction').disabled = !renewal.enabled && (current.source !== 'paid' || !current.ends_at || current.status !== 'active');}
    if ($('suspendBilling')) $('suspendBilling').textContent = current.status === 'suspended' ? 'Возобновить доступ' : 'Приостановить доступ';
    $('openCancellation').disabled = ['expired','cancelled'].includes(current.status);
    $('activePackages').replaceChildren();
    for (const pack of current.packages || []) {const row = el('div','','billing-package-row'), name = el('div',pack.name); name.append(el('small','До ' + date(pack.ends_at))); row.append(name,el('strong',`+${number(pack.quantity)} ${pack.resource === 'sms' ? 'SMS' : 'звонков'}`)); $('activePackages').append(row);}
    if (!(current.packages || []).length) $('activePackages').append(el('p','Пакеты пока не подключены.','billing-empty'));
    offerLink($('currentOfferLink'),current.offer || catalog.offer); offerLink($('acceptedOfferLink'),current.accepted_offer);
    const scheduled = $('scheduledChange'); scheduled.replaceChildren();
    for (const term of current.scheduled_terms || []) {scheduled.append(el('strong',term.plan?.name || 'Следующий тариф'),el('p','Начнёт действовать ' + date(term.starts_at)));}
    if (current.scheduled_change) {const change = current.scheduled_change; scheduled.append(el('strong','Запланированная смена ожидает оплаты'),el('p',`Начало: ${date(change.starts)} · ${money(change.amount_kopecks)}. До оплаты условия не изменятся.`),button('Отменить заявку','billing-text-button',async () => {await request('/admin/api/subscriptions/cancel-scheduled',{branch_id:branchId}); await refresh();}));}
    scheduled.hidden = !scheduled.childNodes.length;
  }
  async function detail() {
    const generation = ++detailGeneration; current = null; $('billingAccount').hidden = true;
    if (branchId) {const data = await request('/admin/api/subscriptions/' + encodeURIComponent(branchId)); if (generation !== detailGeneration) return; current = data.subscription; catalog = data; $('billingAccount').hidden = false; renderAccount();}
    if (generation === detailGeneration) await history();
  }
  async function refresh() {
    const previous = branchId, data = await request('/admin/api/subscriptions'); branches = data.branches; catalog = data; testActivationAvailable = data.test_activation_available === true;
    if (view === 'overview') {const select = $('billingBranch'); select.replaceChildren(); if (superUser) option(select,'','Все филиалы'); branches.forEach(b => option(select,String(b.branch_id),b.name)); branchId = branches.some(b => String(b.branch_id) === String(previous)) ? String(previous) : superUser ? '' : String(branches[0]?.branch_id || ''); select.value = branchId; renderOverview(); await detail();}
    else {branchId = !superUser ? String(branches[0]?.branch_id || '') : ''; current = branches.find(b => String(b.branch_id) === branchId) || null; renderCatalog();}
  }
  const nameFor = id => catalog.plans.find(p => p.id === id)?.name || catalog.packages.find(p => p.id === String(id).replace(/^package:/,''))?.name || id || '—';
  async function history() {
    if (!$('billingHistory')) return;
    const generation = ++historyGeneration, params = new URLSearchParams({page:historyPage}); if (branchId) params.set('branch_id',branchId);
    const form = $('billingHistoryFilters');
    if (form.elements.from.value) params.set('from',new Date(form.elements.from.value + 'T00:00:00').toISOString());
    if (form.elements.to.value) {const to = new Date(form.elements.to.value + 'T00:00:00'); to.setDate(to.getDate()+1); params.set('to',to.toISOString());}
    const usage = ['calls','sms'].includes(historyKind); params.set(usage ? 'resource' : 'kind',historyKind);
    $('billingHistoryPrevious').disabled = $('billingHistoryNext').disabled = true;
    const data = await request('/admin/api/subscriptions/' + (usage ? 'usage?' : 'history?') + params); if (generation !== historyGeneration) return;
    const titles = historyKind === 'payments' ? ['Дата','За что','Сумма','Статус'] : historyKind === 'operations' ? ['Дата','Операция','Подробности'] : ['Дата',historyKind === 'calls' ? 'Ворота / номер' : 'Получатель','Клиент','Списано','Статус'];
    if (superUser) titles.splice(1,0,'Филиал'); const header = el('tr'); titles.forEach(t => header.append(el('th',t))); $('billingHistoryHead').replaceChildren(header); $('billingHistory').replaceChildren();
    for (const item of data.items) {
      const values = [date(item.happened_at ?? item.created ?? item.at)]; if (superUser) values.push(branches.find(b => String(b.branch_id) === String(item.branch_id))?.name || item.branch_id);
      if (historyKind === 'payments') values.push(item.name || nameFor(item.plan_id),money(item.amount_kopecks),labels[item.status] || 'Оплачен');
      else if (historyKind === 'operations') {const d = item.details || {}; values.push(operations[item.event_type] || item.event_type,[d.name || nameFor(d.plan_id || d.package_id || d.item_id),d.amount_kopecks != null ? money(d.amount_kopecks) : '',d.quantity != null ? number(d.quantity) + ' ' + (labels[d.resource] || '') : '',d.reason || ''].filter(v => v && v !== '—').join(' · ') || '—');}
      else values.push([item.gate_name || '',item.phone || 'Номер не сохранён'].filter(Boolean).join(' · '),item.client_name || '—',number(item.charged ?? item.units ?? 0),labels[item.status] || item.status);
      const tr = el('tr'); values.forEach(value => tr.append(el('td',value))); $('billingHistory').append(tr);
    }
    $('billingHistoryEmpty').hidden = Boolean(data.items.length); $('billingHistoryEmpty').textContent = historyKind === 'payments' ? 'Оплат за этот период нет. Неоплаченные заказы отображаются в операциях с тарифом.' : 'За выбранный период операций нет.';
    $('billingHistoryCount').textContent = data.total ? `${(historyPage-1)*data.page_size+1}–${Math.min(historyPage*data.page_size,data.total)} из ${data.total}` : '0 записей';
    $('billingHistoryPrevious').disabled = historyPage <= 1; $('billingHistoryNext').disabled = historyPage * data.page_size >= data.total;
  }
  function renderCatalog() {
    const target = $('catalogCards'); if (!target) return; target.replaceChildren();
    const plans = view === 'plans', items = plans ? catalog.plans : catalog.packages;
    const context = $('catalogContext'); context.replaceChildren();
    const active = current && !['expired','cancelled','pending'].includes(current.status);
    if (!superUser) context.textContent = !catalog.offer ? 'Оферта пока не опубликована. Оформление заказов будет доступно после её публикации.' : active ? `Ваш тариф: ${current.plan?.name || labels[current.source]}. Действует до ${date(current.ends_at)}.` : current ? 'Действующего тарифа нет. Выберите тариф для подключения.' : 'Нет доступного филиала.';
    if ($('archiveCount')) $('archiveCount').textContent = items.filter(i => !i.active).length;
    for (const item of [...items].sort((a,b) => a.price_kopecks-b.price_kopecks || a.name.localeCompare(b.name,'ru')).filter(i => (i.active || $('showArchived')?.checked) && (plans || packageFilter === 'all' || i.resource === packageFilter))) {
      const card = el('article','','billing-catalog-card'); card.classList.toggle('is-current',Boolean(plans && active && current?.plan_id === item.id)); card.classList.toggle('is-archived',!item.active);
      const heading = el('div','','billing-card-heading'), picture = el('div','','billing-logo'); logo(picture,item); heading.append(picture,el('h2',item.name));
      const price = el('div','','billing-card-price'); price.append(el('strong',item.kind === 'trial' ? 'Бесплатно' : money(item.price_kopecks)),el('span',plans ? `за ${duration(item)}` : 'до конца текущего тарифа'));
      const resources = el('ul','','billing-card-resources'); for (const resource of plans ? ['calls','sms'] : [item.resource]) {const li = el('li'); const value = plans ? resource === 'calls' ? item.quota : item.sms_quota : item.quantity; li.append(icon(resource),el('span',`${number(value)} ${resource === 'sms' ? 'SMS' : 'звонков'}`)); resources.append(li);}
      if (plans && item.personalization) resources.append(el('li','Персонализация клиентских страниц'));
      const actions = el('div','','billing-card-actions');
      if (superUser) {
        actions.append(button('Изменить','btn-action',() => editItem(item)));
        if (item.kind !== 'trial') actions.append(button(item.active ? 'В архив' : 'Восстановить','billing-text-button',() => setArchived(item,item.active)));
        const note = !item.active ? 'В архиве' : item.kind === 'trial' ? 'Один раз для нового филиала' : 'Доступен для подключения';
        actions.append(el('p',note,'billing-card-note'));
        if (plans) actions.append(el('span',item.visible === false ? 'Скрыт от филиалов' : 'Виден филиалам','badge billing-visibility'));
      }
      else if (item.kind === 'trial') actions.append(el('p','Предоставляется один раз при регистрации. Не продаётся и не продлевается.','billing-card-note'));
      else {const buy = button(plans ? active && current?.plan_id === item.id ? 'Продлить' : 'Выбрать тариф' : 'Подключить пакет','btn-primary',() => openPurchase(item,plans ? 'plan' : 'package')); let reason = !branchId ? 'Нет доступного филиала' : !catalog.offer ? 'Оферта пока не опубликована' : '';
        if (!plans && !reason) reason = !current?.ends_at || ['expired','cancelled','suspended'].includes(current.status) ? 'Сначала подключите действующий тариф' : current[item.resource]?.quota == null ? 'Безлимит уже включён' : current.packages_available === false ? current.packages_unavailable_reason || 'Подключите тариф из нового каталога' : '';
        buy.disabled = Boolean(reason); actions.append(buy); if (reason) actions.append(el('p',reason,'billing-card-note'));
      }
      card.append(heading,price,resources,actions); target.append(card);
    }
    if (!target.childNodes.length) target.append(el('p','Предложений пока нет.','billing-empty'));
  }
  async function setArchived(item, archived) {
    await request('/admin/api/billing/catalog/' + (view === 'plans' ? 'plan' : 'package'),{id:item.id,revision:item.revision,active:!archived});
    notify(archived ? 'Предложение перемещено в архив. Уже подключённые условия сохранены.' : 'Предложение восстановлено из архива.'); await refresh();
  }
  async function openPurchase(item,kind) {
    const form = $('purchaseForm'); form.reset(); purchase = {item,kind,key:crypto.randomUUID()}; $('purchaseTitle').textContent = kind === 'plan' ? 'Подключение тарифа' : 'Дополнительный пакет';
    const active = ['active','trial'].includes(current?.status) && current?.ends_at && new Date(current.ends_at) > new Date(); form.elements.mode.value = active ? 'period_end' : 'now'; $('purchaseMode').hidden = kind !== 'plan' || !active;
    $('purchaseRenewal').hidden = kind !== 'plan'; $('purchaseScheduledDate').textContent = 'После ' + date(current?.ends_at); $('purchaseError').hidden = true; $('purchaseDialog').showModal(); await updateQuote();
  }
  async function updateQuote() {
    if (!purchase) return; const state = purchase, generation = ++quoteGeneration, form = $('purchaseForm'); $('purchaseConfirm').disabled = true;
    try {const q = await request('/admin/api/subscriptions/' + (state.kind === 'plan' ? 'quote' : 'package-quote'),{branch_id:branchId,[state.kind === 'plan' ? 'plan_id' : 'package_id']:state.item.id,...(state.kind === 'plan' ? {mode:form.elements.mode.value} : {})}); if (generation !== quoteGeneration || purchase !== state) return; state.quote = q; state.key = crypto.randomUUID();
      $('purchaseDetails').replaceChildren(el('h3',state.item.name),el('strong',money(q.amount_kopecks)),el('p',`${date(q.starts_at)} — ${date(q.ends_at)}`));
      $('purchaseBurn').hidden = !q.burn_required; form.elements.confirm_burn.required = Boolean(q.burn_required); form.elements.confirm_burn.checked = false;
      $('purchaseBurnText').textContent = `Подтверждаю обнуление текущих остатков: ${number(q.burn?.calls_remaining)} звонков и ${number(q.burn?.sms_remaining)} SMS. Новый период начнётся после оплаты.`;
      offerLink($('purchaseOffer'),q.offer || catalog.offer); $('purchaseConfirm').disabled = !(q.offer || catalog.offer);
      $('purchaseRenewalText').textContent = `Разрешаю автоматическое продление и списание ${money(state.item.price_kopecks)} каждые ${duration(state.item)}. Согласие можно отозвать в любой момент в разделе «Моя подписка».`;
    } catch (error) {if (generation === quoteGeneration) {$('purchaseError').textContent = error.message; $('purchaseError').hidden = false;} }
  }
  on($('purchaseForm'),'submit',async () => {if (!purchase?.quote) return; const state = purchase, f = $('purchaseForm').elements;
    const data = {branch_id:branchId,[state.kind === 'plan' ? 'plan_id' : 'package_id']:state.item.id,request_id:state.key,offer_version:(state.quote.offer || catalog.offer).id,accept_offer:f.accept_offer.checked};
    if (state.kind === 'plan') Object.assign(data,{mode:f.mode.value,auto_renew:f.auto_renew.checked,confirm_burn:f.confirm_burn.checked});
    const result = await request('/admin/api/subscriptions/' + (state.kind === 'plan' ? 'orders' : 'package-orders'),data);
    const checkout = await request('/admin/api/subscriptions/orders/' + encodeURIComponent(result.order.id) + '/checkout',{branch_id:branchId}); closeDialog($('purchaseDialog')); notify(checkout.message); await refresh();
  });
  $('purchaseForm')?.querySelectorAll('[name=mode]').forEach(input => on(input,'change',updateQuote));
  function editItem(item = null) {
    editing = {item,logo:item?.logo || ''}; const f = $('catalogEditorForm'); f.reset(); const plans = view === 'plans'; $('catalogEditorTitle').textContent = (item ? 'Изменить ' : 'Создать ') + (plans ? 'тариф' : 'пакет');
    root.querySelectorAll('[data-plan-field]').forEach(n => n.hidden = !plans); root.querySelectorAll('[data-package-field]').forEach(n => n.hidden = plans);
    for (const key of ['name','duration_value','duration_unit','quota','sms_quota','resource','quantity']) if (item && item[key] != null) f.elements[key].value = item[key];
    f.elements.price.value = (item?.price_kopecks || 0) / 100; f.elements.visible.checked = item?.visible !== false; f.elements.personalization.checked=Boolean(item?.personalization);
    f.elements.calls_unlimited.checked = Boolean(item && item.quota === null); f.elements.sms_unlimited.checked = Boolean(item && item.sms_quota === null);
    f.elements.price.disabled = item?.kind === 'trial'; $('trialEditorNote').hidden = item?.kind !== 'trial'; f.elements.quota.disabled = f.elements.calls_unlimited.checked; f.elements.sms_quota.disabled = f.elements.sms_unlimited.checked;
    logo($('editorLogo'),item); f.querySelector('[data-dialog-error]').hidden = true; $('catalogEditor').showModal();
  }
  on($('createCatalogItem'),'click',() => editItem()); on($('removeCatalogLogo'),'click',() => {editing.logo = ''; $('catalogEditorForm').elements.image.value = ''; logo($('editorLogo'),{name:$('catalogEditorForm').elements.name.value});});
  for (const [flag,field] of [['calls_unlimited','quota'],['sms_unlimited','sms_quota']]) $('catalogEditorForm')?.elements[flag].addEventListener('change',e => {$('catalogEditorForm').elements[field].disabled = e.target.checked;});
  on($('catalogEditorForm'),'submit',async () => {const f = $('catalogEditorForm').elements, item = editing.item, kind = view === 'plans' ? 'plan' : 'package';
    if (f.image.files.length) {const upload = new FormData(); upload.append('image',f.image.files[0]); editing.logo = (await request('/admin/api/billing/logo',upload)).logo;}
    const data = {name:f.name.value,price_kopecks:Math.round(Number(f.price.value)*100),active:item ? item.active : true,logo:editing.logo}; if (item) Object.assign(data,{id:item.id,revision:item.revision});
    if (kind === 'plan') Object.assign(data,{visible:f.visible.checked,personalization:f.personalization.checked,duration_value:Number(f.duration_value.value),duration_unit:f.duration_unit.value,quota:f.calls_unlimited.checked ? null : Number(f.quota.value),sms_quota:f.sms_unlimited.checked ? null : Number(f.sms_quota.value)}); else Object.assign(data,{resource:f.resource.value,quantity:Number(f.quantity.value)});
    await request('/admin/api/billing/catalog/' + kind,data); closeDialog($('catalogEditor')); notify('Изменения сохранены. Уже подключённые условия не изменились.'); await refresh();
  });
  on($('renewalAction'),'click',async () => {if (current.renewal?.enabled) {await request('/admin/api/subscriptions/renewal',{branch_id:branchId,enabled:false}); notify('Автопродление отключено. Текущий срок и остатки сохранены.'); await refresh();} else {if (!catalog.offer) throw Error('Оферта пока не опубликована'); $('renewalForm').reset(); $('renewalConsentText').textContent = `${current.plan.name}: ${money(current.plan.price_kopecks)} каждые ${duration(current.plan)}. Следующий период — после ${date(current.ends_at)}.`; offerLink($('renewalOfferLink'),catalog.offer); $('renewalDialog').showModal();}});
  on($('renewalForm'),'submit',async () => {const f = $('renewalForm').elements; await request('/admin/api/subscriptions/renewal',{branch_id:branchId,enabled:true,consent:f.consent.checked,accept_offer:f.accept_offer.checked,offer_version:catalog.offer.id}); closeDialog($('renewalDialog')); notify('Согласие на автопродление сохранено. Приём платежей пока не подключён.'); await refresh();});
  on($('openCancellation'),'click',() => {$('cancellationForm').reset(); $('cancelBurn').hidden = true; $('cancellationForm').elements.confirm_burn.required = false; $('cancellationDialog').showModal();});
  $('cancellationForm')?.querySelectorAll('[name=when]').forEach(n => n.addEventListener('change',() => {const now = $('cancellationForm').elements.when.value === 'now'; $('cancelBurn').hidden = !now; $('cancellationForm').elements.confirm_burn.required = now;}));
  on($('cancellationForm'),'submit',async () => {const f = $('cancellationForm').elements; await request('/admin/api/subscriptions/cancel',{branch_id:branchId,immediate:f.when.value === 'now',confirm_burn:f.confirm_burn.checked}); closeDialog($('cancellationDialog')); notify('Настройки подписки сохранены. Автоматических списаний не будет.'); await refresh();});
  function giftOptions() {const f = $('giftForm').elements, plan = f.kind.value === 'plan'; f.item_id.replaceChildren(); (plan ? catalog.plans.filter(p => p.kind !== 'trial') : catalog.packages).filter(p => p.active).forEach(p => option(f.item_id,p.id,p.name)); $('giftTariffFields').hidden = !plan; $('giftPackageHint').hidden = plan; $('giftBurn').hidden = !plan || f.mode.value !== 'replace'; f.confirm_burn.required = plan && f.mode.value === 'replace';}
  on($('openGift'),'click',() => {$('giftForm').reset(); giftOptions(); $('giftDialog').showModal();});
  for (const name of ['kind','mode']) $('giftForm')?.elements[name].addEventListener('change',giftOptions);
  on($('giftForm'),'submit',async () => {const f = $('giftForm').elements, plan = f.kind.value === 'plan', data = {branch_id:branchId,[plan ? 'plan_id' : 'package_id']:f.item_id.value,reason:f.reason.value};
    if (plan) {Object.assign(data,{mode:f.mode.value,confirm_burn:f.confirm_burn.checked}); for (const key of ['days','quota','sms_quota']) if (f[key].value !== '') data[key] = Number(f[key].value); if (f.calls_unlimited.checked) data.quota = null; if (f.sms_unlimited.checked) data.sms_quota = null;}
    await request('/admin/api/subscriptions/gifts/' + (plan ? 'tariff' : 'package'),data); closeDialog($('giftDialog')); notify('Подарок предоставлен филиалу.'); await refresh();
  });
  on($('suspendBilling'),'click',async () => {await request('/admin/api/subscriptions/suspend',{branch_id:branchId,suspended:current.status !== 'suspended'}); await refresh();});
  function testActivationBurn() {
    const f = $('testActivationForm').elements, burns = f.mode.value === 'now' && current?.ends_at && !['expired','cancelled','pending'].includes(current.status);
    $('testActivationBurn').hidden = !burns; f.confirm_burn.required = Boolean(burns); f.confirm_burn.checked = false;
  }
  on($('openTestActivation'),'click',() => {
    if (!testActivationAvailable || !branchId) return;
    const form = $('testActivationForm'); form.reset(); form.elements.plan_id.replaceChildren();
    catalog.plans.filter(plan => plan.active && plan.kind !== 'trial').forEach(plan => option(form.elements.plan_id,plan.id,plan.name + (plan.visible === false ? ' · скрытый' : '')));
    $('testActivationBranch').textContent = 'Филиал: ' + (branches.find(b => String(b.branch_id) === branchId)?.name || branchId);
    testActivationKey = crypto.randomUUID(); testActivationBurn(); $('testActivationDialog').showModal();
  });
  $('testActivationForm')?.elements.mode.addEventListener('change',testActivationBurn);
  on($('testActivationForm'),'submit',async () => {
    const f = $('testActivationForm').elements;
    const result = await request('/admin/api/subscriptions/test-activate',{branch_id:branchId,plan_id:f.plan_id.value,mode:f.mode.value,request_id:testActivationKey,confirm_test:f.confirm_test.checked,confirm_burn:f.confirm_burn.checked});
    closeDialog($('testActivationDialog')); notify(new Date(result.starts_at) > new Date() ? 'Тестовое подключение без оплаты запланировано на ' + date(result.starts_at) + '.' : 'Тариф подключён для тестирования без оплаты. Квоты и срок активированы.'); await refresh();
  });
  on($('billingRefresh'),'click',refresh); on($('billingBranch'),'change',async () => {branchId = $('billingBranch').value; historyPage = 1; await detail();}); $('billingSearch')?.addEventListener('input',renderOverview);
  on($('billingHistoryFilters'),'submit',async () => {historyPage = 1; await history();}); on($('allBillingHistory'),'click',async () => {$('billingHistoryFilters').reset(); historyPage = 1; await history();});
  on($('billingHistoryPrevious'),'click',async () => {historyPage--; await history();}); on($('billingHistoryNext'),'click',async () => {historyPage++; await history();});
  root.querySelectorAll('[data-history]').forEach(n => on(n,'click',async () => {historyKind = n.dataset.history; historyPage = 1; root.querySelectorAll('[data-history]').forEach(b => b.setAttribute('aria-selected',String(b === n))); await history();}));
  root.querySelectorAll('[data-package-filter]').forEach(n => on(n,'click',() => {packageFilter = n.dataset.packageFilter; root.querySelectorAll('[data-package-filter]').forEach(b => b.setAttribute('aria-pressed',String(b === n))); renderCatalog();})); $('showArchived')?.addEventListener('change',renderCatalog);
  if ($('billingHistoryFilters')) {const today = new Date(), before = new Date(); before.setDate(before.getDate()-30); const local = d => new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,10); $('billingHistoryFilters').elements.from.value = local(before); $('billingHistoryFilters').elements.to.value = local(today);}
  refresh().catch(error => notify(error.message,true));
})();
