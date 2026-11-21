"""Client feedback and durable, person-scoped review prompts."""
from datetime import datetime
from flask import Blueprint, abort, jsonify, render_template, request
from storage.sql_documents import read_document
from timeutil import timezone


def prepare(db):
    db.execute('''CREATE TABLE IF NOT EXISTS client_feedback (
        id INTEGER PRIMARY KEY, person_id TEXT NOT NULL, branch_id TEXT NOT NULL,
        stars INTEGER CHECK(stars BETWEEN 1 AND 5), message TEXT NOT NULL, created REAL NOT NULL,
        author_name TEXT NOT NULL DEFAULT '')''')
    if 'author_name' not in {row['name'] for row in db.execute('PRAGMA table_info(client_feedback)')}:
        db.execute("ALTER TABLE client_feedback ADD COLUMN author_name TEXT NOT NULL DEFAULT ''")
        db.execute("""UPDATE client_feedback SET author_name=COALESCE(
            (SELECT name FROM persons WHERE id=client_feedback.person_id),'')""")
    db.execute('CREATE INDEX IF NOT EXISTS feedback_by_person ON client_feedback(person_id,created)')
    db.execute('''CREATE TABLE IF NOT EXISTS client_review_prompts (
        request_id TEXT PRIMARY KEY, branch_id TEXT NOT NULL, person_id TEXT NOT NULL)''')


def due(db, person, branch, token, request_id):
    if db.execute('SELECT 1 FROM client_feedback WHERE person_id=? LIMIT 1', (person,)).fetchone():
        return False
    # The modem confirms that a command was sent. GateBot has no position sensor.
    # Use the customer's admitted requests in ledger order, across devices and bots.
    current = db.execute('''SELECT r.rowid FROM gate_requests r
        JOIN access_request_auth a ON a.request_id=r.id
        WHERE r.id=? AND a.token=? AND r.branch_id=? AND r.status IN ('queued','started','command_sent')''',
        (request_id, token, branch)).fetchone()
    if not current:
        return False
    identity = (branch, person, token)
    clause = 'branch_id=? AND (person_id=? OR branch_client_id=?)'
    first = db.execute('SELECT MIN(rowid) FROM gate_requests WHERE '+clause+
                       " AND status='command_sent' AND rowid<?", (*identity, current[0])).fetchone()[0]
    if first is None:
        return False
    attempts = db.execute('SELECT COUNT(*) FROM gate_requests WHERE '+clause+
                          " AND command_id IS NOT NULL AND status NOT IN ('denied','cancelled','simulated','expired') AND rowid>? AND rowid<=?",
                          (*identity, first, current[0])).fetchone()[0]
    if attempts % 4 != 1:
        return False
    return bool(db.execute('INSERT OR IGNORE INTO client_review_prompts VALUES(?,?,?)',
                           (request_id, branch, person)).rowcount)


def list_reviews(db, limit=None, offset=0):
    prepare(db)
    branches = read_document(db, 'branches.json')
    sql = """SELECT f.id,f.branch_id,f.stars,f.message,f.created,
        COALESCE(NULLIF(f.author_name,''),NULLIF(p.name,''),'Клиент') AS author
        FROM client_feedback f LEFT JOIN persons p ON p.id=f.person_id ORDER BY f.id DESC"""
    rows = db.execute(sql if limit is None else sql + ' LIMIT ? OFFSET ?', () if limit is None else (limit, offset))
    return [{**dict(row), 'branch': branches.get(row['branch_id'], {}).get('name') or row['branch_id']} for row in rows]


def create_blueprint(provider):
    bp = Blueprint('client_feedback', __name__)

    @bp.post('/api/client/feedback/<action>')
    def submit(action):
        if action not in {'prompt', 'send'}:
            abort(404)
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get('token'), str):
            abort(400)
        identity = provider()['service']
        token = data['token']
        with identity.product.connect() as db:
            user = read_document(db, 'links.json').get(token)
            if not user or str(user['branch_id']) not in read_document(db, 'branches.json'):
                abort(404)
            branch = str(user['branch_id'])
            person = identity._client(db, token, user)
            prepare(db)
            if action == 'prompt':
                request_id = data.get('request_id')
                if not isinstance(request_id, str) or len(request_id) > 80:
                    abort(400)
                return jsonify(show=due(db, person, branch, token, request_id))
            stars, message = data.get('stars'), data.get('message', '')
            if (stars is not None and (type(stars) is not int or not 1 <= stars <= 5)
                    or not isinstance(message, str) or len(message) > 2000
                    or not stars and not message.strip()):
                return jsonify(message='Выберите оценку или напишите сообщение'), 400
            at = identity.clock()
            if db.execute('SELECT 1 FROM client_feedback WHERE person_id=? AND created>?', (person, at-30)).fetchone():
                return jsonify(message='Отзыв уже получен'), 429
            db.execute('INSERT INTO client_feedback(person_id,branch_id,stars,message,created,author_name) VALUES(?,?,?,?,?,?)',
                       (person, branch, stars, message.strip(), at, str(user.get('user_name') or '')[:200]))
        return jsonify(message='Спасибо за отзыв!')

    @bp.get('/admin/feedback')
    def inbox():
        services = provider()
        admin = services['auth'].get_current_admin()
        if not admin or not admin.get('is_super'):
            abort(403)
        page = max(1, min(request.args.get('page', 1, type=int), 100000))
        with services['service'].product.connect() as db:
            reviews = list_reviews(db, 51, (page-1)*50)
        for entry in reviews:
            entry['date'] = datetime.fromtimestamp(entry['created'], timezone()).strftime('%d.%m.%Y %H:%M')
        return render_template('admin/feedback.html', admin_page=True, is_super=True, reviews=reviews[:50],
                               page=page, has_next=len(reviews)>50)

    return bp
