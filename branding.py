"""Installation branding, stored in SQLite with sanitized public image assets."""
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import time

from flask import Blueprint, Response, abort, jsonify, request, send_file, session

import logger
from branding_images import FRAME_DEFAULTS, LOGO_FRAME, canonical_brand, frame_settings, icon_png, rasterize_svg, sanitize_svg, submitted_frame
from branch_logos import MAX_UPLOAD_BYTES


ASSET_PREFIX = '/ui/branding/assets/'
DEFAULT_ICON = Path(__file__).resolve().parent/'static'/'images'/'favicon.svg'
DEFAULT_FRAME = FRAME_DEFAULTS
LEGACY_FRAME = {'fit':'contain', 'padding':10, 'background':'#FFFFFF'}


@lru_cache(maxsize=12)
def default_icon_png(svg, size):
    cleaned, aspect = sanitize_svg(svg)
    return icon_png(rasterize_svg(cleaned, aspect), size)


class BrandingSettings:
    def __init__(self, identity):
        self.identity, self.store = identity, identity.product
        self.directory = Path(self.store.path).parent/'branding-assets'
        with self.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS ui_branding_settings(id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL,logo TEXT NOT NULL,icon TEXT NOT NULL,updated REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS ui_branding_assets(filename TEXT PRIMARY KEY,mimetype TEXT NOT NULL)')

    def _super(self, db, username):
        if self.identity._admin(db, username).get('is_super') is not True:
            raise PermissionError('Недостаточно прав для изменения оформления сервиса')

    @staticmethod
    def _state(db):
        row = db.execute('SELECT * FROM ui_branding_settings WHERE id=1').fetchone()
        return (row['revision'], json.loads(row['logo']), json.loads(row['icon']), row['updated']) if row else (0, {}, {}, None)

    @staticmethod
    def _view(state):
        revision, logo, icon, updated = state
        version = hashlib.sha256(b'icon-frame-v2:' + DEFAULT_ICON.read_bytes()).hexdigest()[:16]
        def asset(entry, key, fallback):
            filename = entry.get(key)
            return ASSET_PREFIX+filename if isinstance(filename, str) and re.fullmatch(r'[a-f0-9]{64}\.(svg|png)', filename) else fallback
        return {'revision':revision, 'updated_at':updated, 'logo_custom':bool(logo), 'icon_custom':bool(icon),
                'icon_revision':hashlib.sha256(json.dumps(icon,sort_keys=True).encode()).hexdigest() if icon else version,
                'icon_frame':frame_settings(icon.get('frame', LEGACY_FRAME if icon else DEFAULT_FRAME)),
                'logo_frame':frame_settings(logo.get('frame', LOGO_FRAME)),
                'logo_source_url':asset(logo, 'original', '/static/images/logo.svg'),
                'icon_source_url':asset(icon, 'original', '/static/images/favicon.svg'),
                'logo_url':asset(logo, 'display', asset(logo, 'original', '/static/images/logo.svg')),
                'icon_url':asset(icon, 'display', asset(icon, 'original', '/static/images/favicon.svg')),
                'icon192_url':asset(icon, '192', '/ui/branding/default/icon-192.png?v='+version),
                'icon512_url':asset(icon, '512', '/ui/branding/default/icon-512.png?v='+version),
                'apple_icon_url':asset(icon, '180', '/ui/branding/default/apple-touch-icon.png?v='+version)}

    def public_view(self):
        with self.store.connect() as db:
            return self._view(self._state(db))

    def read(self, username):
        with self.store.connect() as db:
            self._super(db, username)
            return self._view(self._state(db))

    def save(self, username, kind, data, revision, *, frame=None):
        if kind not in {'logo','icon'}:
            raise ValueError('Выберите логотип или иконку')
        if type(revision) is not int or revision < 0:
            raise ValueError('Обновите страницу перед сохранением')
        with self.store.connect() as db:
            self._super(db, username)
            current, logo, icon, _ = self._state(db)
            if current != revision:
                raise ValueError('Оформление изменилось в другом окне. Обновите страницу.')
        if frame is not None:
            frame = frame_settings(frame)
            if data is None:
                name = (icon if kind=='icon' else logo).get('original')
                default = DEFAULT_ICON if kind=='icon' else DEFAULT_ICON.with_name('logo.svg')
                path = self.directory/name if name and re.fullmatch(r'[a-f0-9]{64}\.(png|svg)',name) else default
                try:
                    data = path.read_bytes()
                except FileNotFoundError:
                    raise ValueError('Загрузите исходное изображение ещё раз') from None
        prepared = {}
        if data is not None:
            original, extension, pixels = canonical_brand(data)
            prepared['original'] = (original, extension)
            if kind == 'icon':
                use_display_frame = frame is not None
                frame = frame or DEFAULT_FRAME
                prepared.update({str(size):(icon_png(pixels, size, **frame), 'png') for size in (180,192,512)})
                if use_display_frame:
                    prepared['display'] = prepared['192']
            else:
                frame = frame or LOGO_FRAME
                if frame != LOGO_FRAME:
                    prepared['display'] = (icon_png(pixels, None, **frame), 'png')
        filenames = {}
        # Hash filenames are immutable. Only committed registry entries become public.
        for key, (content, extension) in prepared.items():
            filename = hashlib.sha256(content).hexdigest()+'.'+extension
            self.directory.mkdir(parents=True, exist_ok=True)
            destination = self.directory/filename
            try:
                with destination.open('xb') as output:
                    output.write(content); output.flush(); os.fsync(output.fileno())
            except FileExistsError:
                if hashlib.sha256(destination.read_bytes()).hexdigest() != filename.split('.')[0]:
                    raise ValueError('Не удалось проверить файл изображения') from None
            filenames[key] = filename
        with self.store.connect() as db:
            self._super(db, username)
            current, logo, icon, _ = self._state(db)
            if current != revision:
                raise ValueError('Оформление изменилось в другом окне. Обновите страницу.')
            entry = {**filenames,'frame':frame} if filenames else {}
            if kind == 'logo': logo = entry
            else: icon = entry
            updated = time.time()
            db.execute('INSERT INTO ui_branding_settings VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,logo=excluded.logo,icon=excluded.icon,updated=excluded.updated',
                       (current+1, json.dumps(logo), json.dumps(icon), updated))
            for filename in filenames.values():
                db.execute('INSERT OR IGNORE INTO ui_branding_assets VALUES(?,?)', (filename,'image/svg+xml' if filename.endswith('.svg') else 'image/png'))
            result = self._view((current+1, logo, icon, updated))
        logger.event('UI_BRANDING_UPDATED', details={'actor_ref':logger.reference(username),'kind':kind,'revision':current+1})
        return result


def create_blueprint(provider):
    bp = Blueprint('branding', __name__)

    def settings():
        return BrandingSettings(provider()['service'])

    def authorize():
        admin = provider()['auth'].get_current_admin()
        if not admin: abort(401)
        if admin.get('is_super') is not True: abort(403)
        return session['admin_username']

    @bp.errorhandler(ValueError)
    def invalid(error):
        return jsonify(status='error', message=str(error)), 400

    @bp.errorhandler(PermissionError)
    def forbidden(error):
        return jsonify(status='error', message='Недостаточно прав для изменения оформления сервиса'), 403

    @bp.get('/admin/api/branding')
    def read():
        actor = authorize()
        return jsonify(settings().read(actor))

    @bp.post('/admin/api/branding/<kind>')
    def upload(kind):
        actor = authorize()
        request.max_content_length = MAX_UPLOAD_BYTES+65536
        upload = request.files.get('image')
        frame=submitted_frame(request.form)
        if (upload is None or not upload.filename) and frame is None: raise ValueError('Выберите изображение')
        try: revision = int(request.form.get('revision',''))
        except (ValueError, TypeError): raise ValueError('Обновите страницу перед сохранением') from None
        data=upload.stream.read(MAX_UPLOAD_BYTES+1) if upload and upload.filename else None
        return jsonify(settings().save(actor,kind,data,revision,frame=frame))

    @bp.post('/admin/api/branding/<kind>/reset')
    def reset(kind):
        actor = authorize()
        data = request.get_json(silent=True)
        if not isinstance(data,dict): abort(400)
        return jsonify(settings().save(actor,kind,None,data.get('revision')))

    @bp.get(ASSET_PREFIX+'<filename>')
    def asset(filename):
        if not re.fullmatch(r'[a-f0-9]{64}\.(svg|png)',filename): abort(404)
        service = settings()
        with service.store.connect() as db:
            registered = db.execute('SELECT mimetype FROM ui_branding_assets WHERE filename=?',(filename,)).fetchone()
        path = service.directory/filename
        if not registered or not path.is_file(): abort(404)
        response = send_file(path,mimetype=registered['mimetype'],conditional=True)
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'none'; sandbox"
        return response

    @bp.get('/ui/branding/default/<filename>')
    def default_asset(filename):
        sizes = {'icon-192.png':192,'icon-512.png':512,'apple-touch-icon.png':180}
        if filename not in sizes: abort(404)
        content = default_icon_png(DEFAULT_ICON.read_bytes(),sizes[filename])
        response = Response(content,mimetype='image/png')
        response.set_etag(hashlib.sha256(content).hexdigest())
        response.headers['Cache-Control'] = 'public, max-age=3600'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response.make_conditional(request)

    return bp
