"""Branch-owned appearance for public pages, licensed by the current tariff."""
from functools import lru_cache
import hashlib
import json
import re

from branding_images import canonical_brand, icon_png
from palette_catalog import load_catalog, current_colors
from palette_settings import PaletteSettings, color_value
from billing_utils import epoch


FONTS = {'system':'system-ui, sans-serif', 'humanist':'Trebuchet MS, sans-serif',
         'serif':'Georgia, serif', 'mono':'Courier New, monospace'}
ASSETS = {'background','barrier','favicon','pwa'}
DEFAULTS = {'font':'system', 'font_size':None, 'radius':None, 'hide_logo':False,
            'banner':'', 'banner_align':'left', 'banner_size':16, 'banner_bg':'#EFF0E8',
            'banner_color':'#0C0F14', 'colors':{}}
CLIENT_STYLES = {'access','client-appearance','client-preferences','client-pwa','gate-qr',
                 'branch-desk','theme','branding','ui','admin','legal'}
SHARED_CONTROLS = set('admin-container admin-content form-group admin-form page-heading page-eyebrow '
    'page-description checkbox-label form-hint identityPage identityContent identityDescription identityNotice '
    'panel badge buttons action-buttons btn-primary btn-action btn-success btn-danger icon-button primary-button '
    'brand branch-logo app-bar app-bar-centered guest-container guest-card guest-symbol error-title user-info '
    'alert alert-error alert-info alert-success alert-warning success error danger active max disabled sheet '
    'desk-panel desk-compose desk-own desk-history desk-send desk-actions desk-check desk-heading desk-status '
    'desk-title desk-tools button-admin button-gate warning'.split())
PUBLIC_COMPONENTS=('access-','pass-','gate-','qr-form','qr-remember','qr-text','qr-gate','qr-pass',
                   'bot-link','bot-mark','client-pwa','client-preference','client-install','client-feedback',
                   'clientOffline','contact-link','preference','feedback-','legal-','branch-banner','custom-client',
                   'desk-message','desk-contact','desk-client','status-message')
STYLE_LABELS={'branch-desk':'Переписка','gate-qr':'Въезд по QR-коду','access':'Страница пропуска',
              'client-pwa':'Клиентское приложение','client-preferences':'Каналы уведомлений',
              'client-appearance':'Оформление филиала','legal':'Документы'}


def public_location(location):
    sheet=location['file'].rsplit('/',1)[-1][:-4]
    if sheet not in CLIENT_STYLES:
        return False
    if location['selector']==':root':
        return location['property']!='--sidebar'
    for selector in location['selector'].split(','):
        names=re.findall(r'[.#]([a-zA-Z_][\w-]*)',re.sub(r'\[[^\]]*\]', '', selector))
        if names and all(name in SHARED_CONTROLS or name.startswith(PUBLIC_COMPONENTS) for name in names):
            return True
        if not names and re.match(r'^(body|html|button|label|input|select|textarea|dialog|a|\*)\b',selector.strip()):
            return True
    return False


@lru_cache(maxsize=1)
def client_colors():
    result={}
    for entry in load_catalog()['entries']:
        if not entry.get('editable',True) or not entry.get('active',True):
            continue
        locations=[loc for loc in entry['locations'] if public_location(loc)]
        if locations:
            source=locations[0]
            sheet=source['file'].rsplit('/',1)[-1][:-4]
            group='Основная палитра' if source['selector']==':root' else STYLE_LABELS.get(sheet,'Общие элементы')
            label=entry['label']
            if ' · ' in label:
                label=label.split(' · ',1)[0]+' · '+source['selector']
            result[entry['id']]={**entry,'locations':locations,'label':label,'group':group}
    return result


def validate_settings(raw):
    if not isinstance(raw,dict) or set(raw) - DEFAULTS.keys():
        raise ValueError('Некорректные параметры оформления')
    result = {**DEFAULTS, **raw}
    if not isinstance(result['font'],str) or result['font'] not in FONTS or result['banner_align'] not in ('left','center','right'):
        raise ValueError('Выберите шрифт и выравнивание из списка')
    for key,minimum,maximum in [('font_size',12,24),('radius',0,40),('banner_size',12,36)]:
        value = result[key]
        if value is None and key!='banner_size':
            continue
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError('Проверьте размеры текста и скругления')
    if type(result['hide_logo']) is not bool or not isinstance(result['banner'],str) or len(result['banner'])>400:
        raise ValueError('Проверьте видимость логотипа и текст баннера (до 400 символов)')
    for key in ('banner_bg','banner_color'):
        result[key] = color_value(result[key])
    colors = result['colors']
    if not isinstance(colors,dict) or colors.keys()-client_colors().keys():
        raise ValueError('Каталог цветов изменился. Обновите страницу')
    result['colors'] = {key:color_value(value) for key,value in colors.items()}
    return result


def prepare_asset(kind, data):
    original, extension, pixels = canonical_brand(data)
    if kind=='favicon' and extension!='svg':
        raise ValueError('Для значка браузера выберите SVG')
    if kind=='pwa':
        return {f'pwa{size}':(icon_png(pixels,size),'png') for size in (180,192,512)}
    return {kind:(original,extension)}


class BranchAppearance:
    def __init__(self, identity, billing):
        self.identity, self.billing, self.store = identity, billing, identity.product
        with self.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS branch_appearance(branch_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,settings TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS branch_appearance_assets(branch_id TEXT NOT NULL,kind TEXT NOT NULL,filename TEXT NOT NULL,content BLOB NOT NULL,PRIMARY KEY(branch_id,kind))')

    def enabled(self, branch_id, db=None):
        if db is None:
            with self.store.connect() as tx:
                return self.enabled(branch_id, tx)
        now = epoch(None)
        term = self.billing._term(db,str(branch_id),now)
        if not term or term['ends'] is None or term['ends']<=now:
            return False
        # A depleted call quota still carries the tariff's appearance entitlement.
        row = db.execute('SELECT c.payload FROM billing_catalog c JOIN billing_accounts a ON a.branch_id=? '
                         'WHERE c.kind="plan" AND c.id=? AND a.suspended=0 AND NOT EXISTS '
                         '(SELECT 1 FROM billing_cancelled_terms WHERE term_id=?)',
                         (str(branch_id),term['plan_id'],term['id'])).fetchone()
        return bool(row and json.loads(row[0]).get('personalization') is True)

    def authorize(self, db, username, branch_id):
        self.identity._scope(db, username, branch_id)
        if not self.enabled(branch_id, db):
            raise PermissionError('Для оформления клиентских страниц нужен тариф с персонализацией')

    def _view(self, db, branch_id):
        row = db.execute('SELECT settings,revision FROM branch_appearance WHERE branch_id=?',(str(branch_id),)).fetchone()
        stored = json.loads(row['settings']) if row else {}
        # Revalidate persisted values before using them in CSS. A former palette
        # entry may legitimately disappear when the application's styles change.
        stored['colors']={key:value for key,value in stored.get('colors',{}).items() if key in client_colors()}
        settings = validate_settings(stored)
        assets = {r['kind']:f"/media/client-appearance/{int(branch_id)}/{r['filename']}"
                  for r in db.execute('SELECT kind,filename FROM branch_appearance_assets WHERE branch_id=?',(str(branch_id),))}
        chrome = {entry['locations'][0]['property']: settings['colors'][key][:7]
                  for key,entry in client_colors().items() if key in settings['colors'] and entry['locations'][0]['selector']==':root'}
        return {'settings':settings,'assets':assets,'revision':row['revision'] if row else 0,
                'background_color':chrome.get('--background','#EFF0E8'), 'theme_color':chrome.get('--surface','#FFFFFF')}

    def public(self, branch_id):
        if not branch_id:
            return None
        with self.store.connect() as db:
            if not db.execute('SELECT 1 FROM branch_appearance WHERE branch_id=?',(str(branch_id),)).fetchone() or not self.enabled(branch_id,db):
                return None
            return self._view(db,branch_id)

    def editor(self, username, branch_id):
        with self.store.connect() as db:
            self.authorize(db,username,branch_id)
            view = self._view(db,branch_id)
            palette = PaletteSettings(self.identity)
            defaults = current_colors(load_catalog(),palette._read(db)[1])
            view['palette']=[{'id':key, 'variable':entry['css_var'], 'label':entry['label'],
                              'group':entry['group'], 'default':defaults[key],
                              'usage':entry['locations'][0]['selector']}
                             for key,entry in client_colors().items()]
            return view

    def save(self, username, branch_id, revision, settings, files, remove):
        if type(revision) is not int or revision<0 or not isinstance(remove,list) or any(not isinstance(k,str) or k not in ASSETS for k in remove):
            raise ValueError('Некорректная версия или список изображений')
        if files.keys()-ASSETS or files.keys() & set(remove):
            raise ValueError('Некорректный список изображений')
        with self.store.connect() as db:
            self.authorize(db,username,branch_id)
        settings = validate_settings(settings)
        images = {}
        for kind,content in files.items():
            images.update(prepare_asset(kind,content))
        with self.store.connect() as db:
            self.authorize(db,username,branch_id)
            row = db.execute('SELECT revision FROM branch_appearance WHERE branch_id=?',(str(branch_id),)).fetchone()
            if revision != (row[0] if row else 0):
                raise ValueError('Оформление уже изменилось. Загрузите актуальные настройки.')
            for kind in remove:
                kinds = ('pwa180','pwa192','pwa512') if kind=='pwa' else (kind,)
                db.executemany('DELETE FROM branch_appearance_assets WHERE branch_id=? AND kind=?',
                               [(str(branch_id),k) for k in kinds])
            for kind,(content,ext) in images.items():
                filename=hashlib.sha256(content).hexdigest()+'.'+ext
                db.execute('INSERT OR REPLACE INTO branch_appearance_assets VALUES(?,?,?,?)',(str(branch_id),kind,filename,content))
            db.execute('INSERT OR REPLACE INTO branch_appearance VALUES(?,?,?)',
                       (str(branch_id),revision+1,json.dumps(settings,ensure_ascii=False)))
            return self._view(db,branch_id)

    def asset(self, branch_id, filename):
        with self.store.connect() as db:
            if self.enabled(branch_id,db):
                row = db.execute('SELECT content FROM branch_appearance_assets WHERE branch_id=? AND filename=?',
                                 (str(branch_id),filename)).fetchone()
                if row:
                    return row[0]


def public_brand(brand, theme):
    if not theme:
        return brand
    result = dict(brand)
    for slot,key in [('favicon','icon_url'),('pwa180','apple_icon_url'),('pwa192','icon192_url'),('pwa512','icon512_url')]:
        if slot in theme['assets']:
            result[key]=theme['assets'][slot]
    return result
