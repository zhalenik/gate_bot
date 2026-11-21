"""Scoped billing catalog, immutable documents and account activity surfaces."""
import json
import os
import re
import time

from flask import abort, jsonify, redirect, request, send_file, session

from billing_service import epoch
from branch_logos import canonical_image, MAX_UPLOAD_BYTES


ASSET_PREFIX = '/admin/billing-assets/'
MAX_PDF_BYTES = 10 * 1024 * 1024
OPERATION_PREFIXES = ('tariff_', 'package_', 'subscription_', 'scheduled_change_',
                      'offer_', 'renewal_', 'trial_', 'gift_', 'quota_gift',
                      'payment_', 'order_', 'billing_timezone_')


def register_routes(bp, *, service, auth_required, super_required, admin, allowed,
                    branch, body, visible_branches, render, purchaser):
    def catalog():
        return service('billing').catalog

    def checked_logo(value):
        if not value:
            return ''
        match = re.fullmatch(re.escape(ASSET_PREFIX) + r'([a-f0-9]{32}\.png)', str(value))
        if not match or not (catalog().asset_directory / match[1]).is_file():
            raise ValueError('Загрузите логотип через форму')
        return value

    def selected_branches():
        value = request.args.get('branch_id')
        if value:
            return [branch(value)]
        return [str(item.branch_id) for item in visible_branches()]

    def page_args():
        try:
            page = int(request.args.get('page', 1))
        except (TypeError, ValueError):
            raise ValueError('Некорректная страница') from None
        if not 1 <= page <= 1000000:
            raise ValueError('Некорректная страница')
        start = epoch(request.args['from']) if request.args.get('from') else 0
        end = epoch(request.args['to']) if request.args.get('to') else time.time() + 86400
        if start >= end:
            raise ValueError('Начало периода должно быть раньше окончания')
        return page, start, end

    @bp.get('/admin/tariffs')
    @auth_required
    def tariffs():
        return render('admin/tariffs.html')

    @bp.get('/admin/packages')
    @auth_required
    def packages():
        return render('admin/packages.html')

    @bp.get('/admin/billing-rules')
    @auth_required
    def billing_rules():
        from billing_rules import read, sections
        with service('billing').store.connect() as db:
            revision, document = read(db)
        return render('admin/billing-rules.html', rules=sections(document), document=document, revision=revision)

    @bp.post('/admin/billing-rules')
    @super_required
    def publish_rules():
        from billing_rules import publish
        with service('billing').store.connect() as db:
            publish(db, request.form.get('revision'), request.form.get('document'))
        return redirect('/admin/billing-rules', code=303)

    @bp.get('/admin/api/billing/catalog')
    @auth_required
    def billing_catalog():
        is_super = admin().get('is_super') is True
        return jsonify(catalog().list(include_archived=is_super, include_hidden=is_super))

    @bp.post('/admin/api/billing/catalog/<kind>')
    @super_required
    def save_catalog_item(kind):
        if kind not in {'plan', 'package'}:
            abort(404)
        data = body()
        if 'logo' in data:
            data['logo'] = checked_logo(data['logo'])
        return jsonify(item=catalog().save(kind, data, actor=session['admin_username']))

    @bp.post('/admin/api/billing/logo')
    @super_required
    def billing_logo():
        request.max_content_length = MAX_UPLOAD_BYTES + 65536
        upload = request.files.get('image')
        if upload is None:
            raise ValueError('Выберите PNG, JPEG или WebP')
        pixels = canonical_image(upload.stream.read(MAX_UPLOAD_BYTES + 1))
        # Recheck the current role after decoding; no uploaded metadata survives.
        if admin().get('is_super') is not True:
            abort(403)
        _, url = catalog().write_asset(pixels, '.png')
        return jsonify(logo=url)

    @bp.post('/admin/api/billing/offer')
    @super_required
    def publish_offer():
        request.max_content_length = MAX_PDF_BYTES + 65536
        upload = request.files.get('document')
        if upload is None:
            raise ValueError('Выберите PDF с договором оферты')
        data = upload.stream.read(MAX_PDF_BYTES + 1)
        if (not data or len(data) > MAX_PDF_BYTES or
                not re.match(rb'%PDF-[12]\.[0-9]', data) or b'%%EOF' not in data[-2048:]):
            raise ValueError('Выберите корректный PDF размером до 10 МБ')
        if admin().get('is_super') is not True:
            abort(403)
        filename = str(upload.filename or '').replace('\\', '/').rsplit('/', 1)[-1]
        filename = ''.join(char for char in filename if ord(char) >= 32)[:120] or 'offer.pdf'
        return jsonify(offer=catalog().save_offer_file(filename, data, actor=session['admin_username']))

    @bp.get('/admin/api/billing/offer')
    @auth_required
    def offer_document():
        return jsonify(offer=catalog().offer(request.args.get('version')))

    @bp.get(ASSET_PREFIX + '<filename>')
    @auth_required
    def billing_asset(filename):
        if not re.fullmatch(r'[a-f0-9]{32}\.(png|pdf)', filename):
            abort(404)
        path = catalog().asset_directory / filename
        if not path.is_file():
            abort(404)
        pdf = filename.endswith('.pdf')
        response = send_file(path, mimetype='application/pdf' if pdf else 'image/png',
                             as_attachment=pdf and request.args.get('download') == '1',
                             download_name='GateBot-offer.pdf' if pdf else 'logo.png')
        response.headers['Cache-Control'] = 'private, max-age=3600'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        if pdf:
            response.headers['Content-Security-Policy'] = "sandbox; default-src 'none'"
        return response

    @bp.post('/admin/api/subscriptions/package-quote')
    @auth_required
    def package_quote():
        purchaser()
        data = body()
        return jsonify(service('billing').package_quote(branch(data.get('branch_id')), data.get('package_id')))

    @bp.post('/admin/api/subscriptions/package-orders')
    @auth_required
    def package_order():
        purchaser()
        data = body()
        order = service('billing').create_package_order(branch(data.get('branch_id')), data.get('package_id'),
            actor=session['admin_username'], idempotency_key=data.get('request_id'),
            offer_version=data.get('offer_version'), accept_offer=data.get('accept_offer') is True)
        return jsonify(order=order), 201

    @bp.post('/admin/api/subscriptions/cancel')
    @auth_required
    def cancel_subscription():
        data = body()
        result = service('billing').cancel_subscription(branch(data.get('branch_id')),
            actor=session['admin_username'], immediate=data.get('immediate') is True,
            confirm_burn=data.get('confirm_burn') is True)
        return jsonify(subscription=result)

    @bp.post('/admin/api/subscriptions/renewal')
    @auth_required
    def renewal():
        purchaser()
        data = body()
        if type(data.get('enabled')) is not bool:
            raise ValueError('Укажите состояние автопродления')
        result = service('billing').set_renewal(branch(data.get('branch_id')), data['enabled'],
            actor=session['admin_username'], consent=data.get('consent') is True,
            offer_version=data.get('offer_version'), accept_offer=data.get('accept_offer') is True)
        return jsonify(subscription=result)

    @bp.post('/admin/api/subscriptions/cancel-scheduled')
    @auth_required
    def cancel_scheduled():
        data = body()
        return jsonify(subscription=service('billing').cancel_scheduled(branch(data.get('branch_id')),
            actor=session['admin_username']))

    @bp.post('/admin/api/subscriptions/gifts/tariff')
    @super_required
    def tariff_gift():
        data = body()
        overrides = {name: data[name] for name in ('quota', 'sms_quota', 'days') if name in data}
        result = service('billing').grant_plan_gift(branch(data.get('branch_id')), data.get('plan_id'),
            actor=session['admin_username'], mode=data.get('mode', 'extend'),
            reason=str(data.get('reason', ''))[:1000], confirm_burn=data.get('confirm_burn') is True, **overrides)
        return jsonify(subscription=result)

    @bp.post('/admin/api/subscriptions/test-activate')
    @super_required
    def test_activate_tariff():
        if os.getenv('GATEBOT_ENV') != 'development':
            abort(403, description='Тестовое подключение доступно только в режиме разработки')
        data = body()
        if data.get('confirm_test') is not True:
            raise ValueError('Подтвердите тестовое подключение без оплаты')
        result = service('billing').activate_test_tariff(branch(data.get('branch_id')), data.get('plan_id'),
            actor=session['admin_username'], idempotency_key=data.get('request_id'),
            mode=data.get('mode', 'now'), confirm_burn=data.get('confirm_burn') is True)
        return jsonify(result)

    @bp.post('/admin/api/subscriptions/gifts/package')
    @super_required
    def package_gift():
        data = body()
        result = service('billing').gift_package(branch(data.get('branch_id')), data.get('package_id'),
            actor=session['admin_username'], reason=str(data.get('reason', ''))[:1000])
        return jsonify(subscription=result)

    @bp.get('/admin/api/subscriptions/usage')
    @auth_required
    def usage():
        resource = request.args.get('resource', 'calls')
        if resource not in {'calls', 'sms'}:
            raise ValueError('Выберите звонки или SMS')
        branches = selected_branches()
        page, start, end = page_args()
        result = service('billing').usage_details(branches, resource=resource,
            from_at=start, to_at=end, page=page, page_size=50)
        # A pass token is never needed to display usage.
        for item in result['items']:
            item.pop('client_token', None)
            for client in item.get('clients', []):
                client.pop('client_token', None)
        return jsonify(result)

    @bp.get('/admin/api/subscriptions/history')
    @auth_required
    def billing_history():
        branches = selected_branches()
        page, start, end = page_args()
        kind = request.args.get('kind', 'payments')
        if kind not in {'payments', 'operations'}:
            raise ValueError('Неизвестный раздел истории')
        if not branches:
            return jsonify(items=[], total=0, page=page, page_size=50)
        placeholders = ','.join('?' for _ in branches)
        with service('billing').store.connect() as db:
            if kind == 'payments':
                source = 'billing_payments p JOIN billing_orders o ON o.id=p.order_id'
                where = f'o.branch_id IN ({placeholders}) AND p.happened_at>=? AND p.happened_at<?'
                args = branches + [start, end]
                total = db.execute('SELECT count(*) FROM ' + source + ' WHERE ' + where, args).fetchone()[0]
                rows = db.execute('SELECT p.*,o.branch_id,o.kind,o.plan_id,o.snapshot FROM ' + source +
                    ' WHERE ' + where + ' ORDER BY p.happened_at DESC,p.event_id LIMIT 50 OFFSET ?',
                    args + [(page - 1) * 50]).fetchall()
                items = []
                for row in rows:
                    item = dict(row)
                    snapshot = json.loads(item.pop('snapshot') or '{}')
                    item['name'] = (snapshot.get('plan') or snapshot.get('package') or {}).get('name') or item['plan_id']
                    item['status'] = 'paid'
                    items.append(item)
            else:
                where = (f'branch_id IN ({placeholders}) AND happened_at>=? AND happened_at<? AND (' +
                         ' OR '.join('event_type LIKE ?' for _ in OPERATION_PREFIXES) + ')')
                args = branches + [start, end] + [prefix + '%' for prefix in OPERATION_PREFIXES]
                total = db.execute('SELECT count(*) FROM product_events WHERE ' + where, args).fetchone()[0]
                rows = db.execute('SELECT * FROM product_events WHERE ' + where +
                    ' ORDER BY happened_at DESC,id LIMIT 50 OFFSET ?', args + [(page - 1) * 50]).fetchall()
                items = []
                for row in rows:
                    item = dict(row)
                    payload = json.loads(item.pop('payload') or '{}')
                    item['details'] = {key: payload[key] for key in ('plan_id', 'package_id', 'name', 'amount_kopecks',
                        'quantity', 'resource', 'quota', 'sms_quota', 'mode', 'ends_at', 'reason', 'actor', 'item_id') if key in payload}
                    items.append(item)
        return jsonify(items=items, total=total, page=page, page_size=50)
