"""Branch-owned appearance for public pages, licensed by the current tariff."""
from functools import lru_cache
import hashlib
import io
import json
import re
from PIL import Image

from branding_images import canonical_brand, icon_png, validate_icon_frame
from palette_catalog import load_catalog, current_colors
from palette_settings import PaletteSettings, color_value
from billing_utils import epoch


FONTS = {'system':'system-ui, sans-serif', 'humanist':'Trebuchet MS, sans-serif',
         'serif':'Georgia, serif', 'mono':'Courier New, monospace',
         **{font.lower():font+', sans-serif' for font in ('Inter','Rubik','Nunito','Mulish')}}
ASSETS = {'background','card_background','barrier','favicon','pwa'}
GLOBAL = 'default'
ICON_FRAME={'fit':'cover','padding':0,'background':'#FFFFFF'}
DEFAULTS = {'font':'system', 'font_size':None, 'radius':None, 'hide_logo':False,
            'button_gap':None, 'button_icon':True,'button_align':'left','title_align':'left',
            'logo_position':'before','logo_align':'center', 'icon_frame':ICON_FRAME,
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
    if not isinstance(result['font'],str) or result['font'] not in FONTS or any(
        result[key] not in ('left','center','right') for key in ('button_align','title_align','logo_align','banner_align')
    ) or result['logo_position'] not in ('before','above'):
        raise ValueError('Выберите шрифт и выравнивание из списка')
    frame=result['icon_frame']
    if not isinstance(frame,dict) or frame.keys()!=ICON_FRAME.keys() or type(result['button_icon']) is not bool:
        raise ValueError('Проверьте настройки иконки')
    result['icon_frame']=validate_icon_frame(**frame)
    for key,minimum,maximum in [('font_size',12,24),('radius',0,40),('button_gap',0,64),('banner_size',12,36)]:
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


def prepare_asset(kind, data, frame):
    original, extension, pixels = canonical_brand(data)
    if kind=='favicon' and extension!='svg':
        raise ValueError('Для значка браузера выберите SVG')
    if kind=='pwa':
        return {'pwa_source':(original,extension),**{f'pwa{size}':(icon_png(pixels,size,**frame),'png') for size in (180,192,512)}}
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
        if branch_id == GLOBAL:
            if self.identity._admin(db,username).get('is_super') is not True:
                raise PermissionError('Недостаточно прав для изменения оформления сервиса')
            return
        self.identity._scope(db, username, branch_id)
        if not self.enabled(branch_id, db):
            raise PermissionError('Для оформления клиентских страниц нужен тариф с персонализацией')

    def _stored(self, db, branch_id):
        row = db.execute('SELECT settings,revision FROM branch_appearance WHERE branch_id=?',(str(branch_id),)).fetchone()
        stored = json.loads(row['settings']) if row else {}
        stored['colors']={key:value for key,value in stored.get('colors',{}).items() if key in client_colors()}
        assets = {r['kind']:f"/media/client-appearance/{branch_id}/{r['filename']}"
                  for r in db.execute('SELECT kind,filename FROM branch_appearance_assets WHERE branch_id=?',(str(branch_id),))}
        if 'pwa512' in assets and 'pwa_source' not in assets:
            # alpha.24 stored only the padded output. Expose the part inside its
            # generated border for reframing without changing the installed icons.
            assets['pwa_source']=assets['pwa512'].replace('/media/client-appearance/','/media/client-icon-source/')
            stored.setdefault('icon_frame',{'fit':'contain','padding':10,'background':'#FFFFFF'})
        return stored, assets, row['revision'] if row else 0

    def _view(self, db, branch_id, *, editing=False):
        base,common_assets,base_revision=self._stored(db,GLOBAL)
        own,own_assets,revision=self._stored(db,branch_id) if branch_id!=GLOBAL else (base,common_assets,base_revision)
        installation={}
        if editing:
            from branding import BrandingSettings
            installation=BrandingSettings(self.identity).public_view()
            base.setdefault('icon_frame',installation.get('icon_frame',ICON_FRAME))
        settings=validate_settings({**base,**own,'colors':{**base['colors'],**own['colors']}})
        assets={**common_assets,**own_assets}
        chrome = {entry['locations'][0]['property']: settings['colors'][key][:7]
                  for key,entry in client_colors().items() if key in settings['colors'] and entry['locations'][0]['selector']==':root'}
        return {'settings':settings,'assets':assets,'revision':revision, 'base_revision':base_revision,
                'own_assets':list(own_assets),'inherited_assets':common_assets if branch_id!=GLOBAL else {},
                'base_settings':validate_settings(base) if branch_id!=GLOBAL else validate_settings({'icon_frame':installation.get('icon_frame',ICON_FRAME)}),
                'default_icon_source':installation.get('icon_url'),
                'background_color':chrome.get('--background','#EFF0E8'), 'theme_color':chrome.get('--surface','#FFFFFF')}

    def public(self, branch_id):
        with self.store.connect() as db:
            if branch_id and db.execute('SELECT 1 FROM branch_appearance WHERE branch_id=?',(str(branch_id),)).fetchone() and self.enabled(branch_id,db):
                return self._view(db,branch_id)
            if db.execute('SELECT 1 FROM branch_appearance WHERE branch_id=?',(GLOBAL,)).fetchone():
                return self._view(db,GLOBAL)
            return None

    def editor(self, username, branch_id):
        with self.store.connect() as db:
            self.authorize(db,username,branch_id)
            view = self._view(db,branch_id,editing=True)
            palette = PaletteSettings(self.identity)
            defaults = current_colors(load_catalog(),palette._read(db)[1])
            view['palette']=[{'id':key, 'variable':entry['css_var'], 'label':entry['label'],
                              'group':entry['group'], 'default':defaults[key],
                              'usage':entry['locations'][0]['selector']}
                             for key,entry in client_colors().items()]
            return view

    def save(self, username, branch_id, revision, settings, files, remove, base_revision=None):
        if type(revision) is not int or revision<0 or not isinstance(remove,list) or any(not isinstance(k,str) or k not in ASSETS for k in remove):
            raise ValueError('Некорректная версия или список изображений')
        if files.keys()-ASSETS or files.keys() & set(remove):
            raise ValueError('Некорректный список изображений')
        settings = validate_settings(settings)
        images = {}
        with self.store.connect() as db:
            self.authorize(db,username,branch_id)
            previous=self._view(db,branch_id,editing=True)
            frame=settings['icon_frame']
            if 'pwa' in remove:
                if frame!=previous['base_settings']['icon_frame']:
                    files={**files,'pwa':self._icon_source(db,GLOBAL if branch_id!=GLOBAL else None)}
            elif 'pwa' not in files and frame!=previous['settings']['icon_frame']:
                files={**files,'pwa':self._icon_source(db,branch_id)}
        for kind,content in files.items():
            images.update(prepare_asset(kind,content,settings['icon_frame']))
        with self.store.connect() as db:
            self.authorize(db,username,branch_id)
            current=self._view(db,branch_id,editing=True)
            if revision != current['revision'] or base_revision is not None and base_revision!=current['base_revision']:
                raise ValueError('Оформление уже изменилось. Загрузите актуальные настройки.')
            baseline=current['base_settings']
            overrides={key:value for key,value in settings.items() if key!='colors' and value!=baseline[key]}
            # Uploaded icons carry their own composition until their source is removed.
            if 'pwa' in files or 'pwa_source' in current['own_assets'] and 'pwa' not in remove:
                overrides['icon_frame']=settings['icon_frame']
            if 'pwa' in remove and 'pwa' not in files:
                overrides.pop('icon_frame',None)
            overrides['colors']={k:v for k,v in settings['colors'].items() if v!=baseline['colors'].get(k)}
            for kind in remove:
                kinds = ('pwa_source','pwa180','pwa192','pwa512') if kind=='pwa' else (kind,)
                db.executemany('DELETE FROM branch_appearance_assets WHERE branch_id=? AND kind=?',
                               [(str(branch_id),k) for k in kinds])
            for kind,(content,ext) in images.items():
                filename=hashlib.sha256(content).hexdigest()+'.'+ext
                db.execute('INSERT OR REPLACE INTO branch_appearance_assets VALUES(?,?,?,?)',(str(branch_id),kind,filename,content))
            db.execute('INSERT OR REPLACE INTO branch_appearance VALUES(?,?,?)',
                       (str(branch_id),revision+1,json.dumps(overrides,ensure_ascii=False)))
            return self._view(db,branch_id,editing=True)

    @staticmethod
    def _legacy_icon(content):
        with Image.open(io.BytesIO(content)) as previous:
            side=round(previous.width*.8)
            offset=(previous.width-side)//2
            inner=previous.crop((offset,offset,offset+side,offset+side))
            output=io.BytesIO()
            inner.save(output,format='PNG')
            return output.getvalue()

    def _icon_source(self,db,branch_id):
        owners=[]
        if branch_id is not None:
            if branch_id!=GLOBAL:
                owners.append(branch_id)
            owners.append(GLOBAL)
        for owner in owners:
            for key in ('pwa_source','pwa512'):
                row=db.execute('SELECT content FROM branch_appearance_assets WHERE branch_id=? AND kind=?',(str(owner),key)).fetchone()
                if row:
                    return row[0] if key=='pwa_source' else self._legacy_icon(row[0])
        from branding import BrandingSettings, DEFAULT_ICON, ASSET_PREFIX
        branding=BrandingSettings(self.identity)
        url=branding.public_view()['icon_url']
        return (branding.directory/url.removeprefix(ASSET_PREFIX)).read_bytes() if url.startswith(ASSET_PREFIX) else DEFAULT_ICON.read_bytes()

    def asset(self, branch_id, filename, legacy=False):
        with self.store.connect() as db:
            if branch_id==GLOBAL or self.enabled(branch_id,db):
                row = db.execute('SELECT content FROM branch_appearance_assets WHERE branch_id=? AND filename=? AND (?=0 OR kind="pwa512")',
                                 (str(branch_id),filename,legacy)).fetchone()
                if row:
                    return self._legacy_icon(row[0]) if legacy else row[0]


def public_brand(brand, theme):
    if not theme:
        return brand
    result = dict(brand)
    for slot,key in [('favicon','icon_url'),('pwa180','apple_icon_url'),('pwa192','icon192_url'),('pwa512','icon512_url')]:
        if slot in theme['assets']:
            result[key]=theme['assets'][slot]
    return result
