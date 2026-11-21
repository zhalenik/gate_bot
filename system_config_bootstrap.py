"""Encrypted restart-only overrides, independent of movable application data.

Reading does not create files. The installation directory is intentionally fixed;
moving DATA_DIR must not hide the settings needed to find that directory.
"""
import json
import os
from pathlib import Path
import secrets

from cryptography.fernet import Fernet, InvalidToken
from filelock import FileLock

from system_settings_catalog import BY_KEY, encode


class SystemConfigStore:
    def __init__(self, directory=None, *, project_dir=None):
        project=Path(project_dir) if project_dir is not None else Path(__file__).resolve().parent
        self.directory=Path(directory) if directory is not None else project/'private'/'system-config'
        self.path=self.directory/'settings.enc'
        self.key_path=self.directory/'settings.key'

    def read(self):
        if not self.path.exists():return {'revision':'0','values':{}}
        try:
            if self.path.stat().st_size>1024*1024:raise ValueError('size')
            fernet=Fernet(self.key_path.read_bytes())
            payload=json.loads(fernet.decrypt(self.path.read_bytes()).decode('utf-8'))
            if not isinstance(payload,dict) or set(payload)!={'version','revision','values'} or payload['version']!=1:
                raise ValueError('shape')
            if not isinstance(payload['revision'],str) or not isinstance(payload['values'],dict):
                raise ValueError('fields')
            # Older encrypted settings can contain the retired console-owner bootstrap field.
            # Ignore it on load and drop it on the next successful save.
            payload['values'].pop('BOOTSTRAP_SUPERADMIN_PHONE',None)
            old_outbound = payload['values'].pop('OUTBOUND_ENABLED', None)
            if old_outbound is not None:
                for key in ('CLIENT_OUTBOUND_ENABLED', 'ADMIN_OUTBOUND_ENABLED'):
                    payload['values'].setdefault(key, old_outbound)
            if set(payload['values'])-set(BY_KEY):
                raise ValueError('fields')
            values={key:encode(BY_KEY[key],value) for key,value in payload['values'].items()}
            return {'revision':payload['revision'],'values':values}
        except (OSError,ValueError,TypeError,InvalidToken,UnicodeError):
            raise RuntimeError('Не удалось прочитать системные настройки. Восстановите settings.enc и settings.key из одной резервной копии.') from None

    def save(self, values, revision):
        if not isinstance(values,dict) or set(values)-set(BY_KEY):raise ValueError('Неизвестные системные параметры')
        encoded={key:encode(BY_KEY[key],value) for key,value in values.items()}
        self.directory.mkdir(parents=True,exist_ok=True)
        with FileLock(str(self.directory/'settings.lock'),timeout=10):
            current=self.read()
            if revision!=current['revision']:raise ValueError('Настройки уже изменились. Обновите страницу и повторите сохранение')
            if not self.key_path.exists():
                # An existing encrypted file without its key never reaches here.
                descriptor=os.open(self.key_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(descriptor,'wb') as stream:
                    stream.write(Fernet.generate_key());stream.flush();os.fsync(stream.fileno())
            payload={'version':1,'revision':secrets.token_hex(16),'values':encoded}
            ciphertext=Fernet(self.key_path.read_bytes()).encrypt(json.dumps(payload,ensure_ascii=False).encode())
            temporary=self.directory/('settings-'+secrets.token_hex(8)+'.tmp')
            try:
                descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(descriptor,'wb') as stream:
                    stream.write(ciphertext);stream.flush();os.fsync(stream.fileno())
                os.replace(temporary,self.path)
            finally:
                if temporary.exists():temporary.unlink()
        return {'revision':payload['revision'],'values':encoded}


def apply_startup_overrides(project_dir, environment=None):
    target=os.environ if environment is None else environment
    saved=SystemConfigStore(project_dir=project_dir).read()
    for key,value in saved['values'].items():target[key]=value
    return saved
