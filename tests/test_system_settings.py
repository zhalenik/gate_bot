"""Private configuration roundtrip and restart behavior; no live credentials."""
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from storage import config
from system_config_bootstrap import SystemConfigStore, apply_startup_overrides
from system_settings import SystemSettings
from system_settings_catalog import BY_KEY, SMTP_ENV_KEYS
from test_identity_product import identities


@pytest.fixture
def settings(identities,tmp_path,monkeypatch):
    base={'GATEBOT_ENV':'development','SECRET_KEY':'initial-offline-session-secret-12345678',
          'GATEBOT_DATA_DIR':str(tmp_path/'state'),'GATEBOT_LOG_DIR':str(tmp_path/'state'/'logs'),
          'BOT_TOKEN':'old-synthetic-token','YC_BASE_URL':'https://api.yclients.ru/api/v1'}
    monkeypatch.setattr(config,'SYSTEM_BASE_ENVIRONMENT',dict(base))
    monkeypatch.setattr(config,'SYSTEM_ACTIVE_ENVIRONMENT',dict(base))
    return SystemSettings(identities.service,SystemConfigStore(tmp_path/'bootstrap'))


def field(snapshot,key):return next(item for item in snapshot['fields'] if item['key']==key)


def test_private_encrypted_overrides_never_hot_apply_or_echo_secrets(settings):
    current=settings.public('owner');old=config.PORT
    result=settings.save('owner',{'revision':current['revision'],'values':{'PORT':6789,'BOT_TOKEN':'new-synthetic-token','DEV_REGISTRATION_SMS_REDIRECT_ENABLED':True}})
    assert result['restart_required'] and config.PORT==old
    assert field(result,'PORT')['value']==6789 and field(result,'PORT')['active_value']==5000
    assert field(result,'BOT_TOKEN')['configured'] and 'value' not in field(result,'BOT_TOKEN')
    assert 'new-synthetic-token' not in json.dumps(result)
    assert 'old-synthetic-token' not in json.dumps(result)
    assert b'new-synthetic-token' not in settings.store.path.read_bytes()
    assert SystemConfigStore(settings.store.directory).read()['values']['BOT_TOKEN']=='new-synthetic-token'
    settings.save('owner',{'revision':result['revision'],'values':{'BOT_TOKEN':''}})
    assert settings.store.read()['values']['BOT_TOKEN']=='new-synthetic-token'


def test_secret_clear_and_reset_are_distinct_and_revision_is_checked(settings):
    revision=settings.public('owner')['revision']
    saved=settings.save('owner',{'revision':revision,'clear':['BOT_TOKEN']})
    assert not field(saved,'BOT_TOKEN')['configured']
    assert settings.store.read()['values']['BOT_TOKEN']==''
    with pytest.raises(ValueError):settings.save('owner',{'revision':revision,'values':{'PORT':9999}})
    reset=settings.save('owner',{'revision':saved['revision'],'reset':['BOT_TOKEN']})
    assert field(reset,'BOT_TOKEN')['configured'] and not field(reset,'BOT_TOKEN')['overridden']
    assert not reset['restart_required']


def test_old_encrypted_bootstrap_phone_and_modem_timeout_survive_upgrade(settings):
    from cryptography.fernet import Fernet
    saved=settings.save('owner',{'revision':'0','values':{'SIM800_COMMAND_TIMEOUT':120}})
    box=Fernet(settings.store.key_path.read_bytes())
    private=json.loads(box.decrypt(settings.store.path.read_bytes()))
    private['values']['BOOTSTRAP_SUPERADMIN_PHONE']='+15550000010'
    settings.store.path.write_bytes(box.encrypt(json.dumps(private).encode()))
    assert settings.store.read()=={'revision':saved['revision'],'values':{'SIM800_COMMAND_TIMEOUT':'120.0'}}


@pytest.mark.parametrize('values',[
    {'PORT':0},{'PORT':True},{'SIM800_CALL_DURATION':'nan'}, {'HOST':'host/path'},
    {'GATEBOT_TIMEZONE':'../../private'}, {'BASE_URL':'https://user:password@example.invalid'},
    {'YC_BASE_URL':'https://example.invalid'}, {'GATEBOT_ENV':'unknown'}, {'SECRET_KEY':'short'},
    {'BOT_TOKEN':'header\r\ninjection'}, {'GATEBOT_DATA_DIR':'bad\x00path'},
    {'YC_USER_TOKEN':'forbidden-global-user-token'}, {'PYTHONPATH':'arbitrary-path'},
])
def test_invalid_or_unknown_configuration_never_writes(settings,values):
    with pytest.raises(ValueError):settings.save('owner',{'revision':'0','values':values})
    assert not settings.store.path.exists()


@pytest.mark.parametrize('url,expected',[
    ('https://api.yclients.ru/api/v1','https://api.yclients.ru/api/v1'),
    ('https://api.yclients.com/api/v1','https://api.yclients.com/api/v1'),
    ('https://api.yclients.com:443/api/v1/','https://api.yclients.com/api/v1'),
])
def test_settings_accept_the_same_official_yclients_addresses_as_reader(settings,monkeypatch,url,expected):
    monkeypatch.setitem(config.SYSTEM_BASE_ENVIRONMENT,'YC_BASE_URL',url)
    monkeypatch.setitem(config.SYSTEM_ACTIVE_ENVIRONMENT,'YC_BASE_URL',url)
    current=settings.public('owner')
    assert field(current,'YC_BASE_URL')['value']==expected
    assert not current['restart_required']
    saved=settings.save('owner',{'revision':current['revision'],'values':{'YC_BASE_URL':url}})
    assert field(saved,'YC_BASE_URL')['value']==expected
    assert not saved['restart_required']


@pytest.mark.parametrize('url',[
    'http://api.yclients.com/api/v1',
    'https://api.yclients.com:444/api/v1',
    'https://api.yclients.com.example.invalid/api/v1',
    'https://api.yclients.com@elsewhere.invalid/api/v1',
    'https://user:password@api.yclients.com/api/v1',
    'https://api.yclients.com/api/v2',
    'https://api.yclients.com/api/v1?token=synthetic-url-token',
    'https://api.yclients.com/api/v1#fragment',
])
def test_official_host_support_does_not_allow_other_destinations(settings,url):
    with pytest.raises(ValueError):settings.save('owner',{'revision':'0','values':{'YC_BASE_URL':url}})
    assert not settings.store.path.exists()


@pytest.mark.parametrize('key,legacy,replacement',[
    ('YC_BASE_URL','https://legacy.example.invalid/api/v1','https://api.yclients.com/api/v1'),
    ('PORT','not-a-number',5001),
    ('SIM800_CALL_DURATION','nan',3.5),
    ('GATEBOT_TIMEZONE','Retired/Timezone','Europe/Moscow'),
])
def test_legacy_environment_can_be_read_and_fixed_while_runtime_keeps_old_value(settings,monkeypatch,key,legacy,replacement):
    monkeypatch.setitem(config.SYSTEM_BASE_ENVIRONMENT,key,legacy)
    monkeypatch.setitem(config.SYSTEM_ACTIVE_ENVIRONMENT,key,legacy)
    current=settings.public('owner')
    assert field(current,key)['source']=='environment'
    assert field(current,key)['value']==legacy and field(current,key)['validation_error']
    assert key in current['invalid_keys'] and key in current['active_invalid_keys']
    assert not settings.store.directory.exists()
    with pytest.raises(ValueError):
        settings.save('owner',{'revision':current['revision'],'values':{'OUTBOUND_ENABLED':True}})
    assert not settings.store.directory.exists()
    saved=settings.save('owner',{'revision':current['revision'],'values':{key:replacement}})
    assert field(saved,key)['value']==replacement
    assert field(saved,key)['restart_required'] and key in saved['restart_keys']
    assert not field(saved,key)['validation_error'] and field(saved,key)['active_validation_error']
    assert key not in saved['invalid_keys'] and key in saved['active_invalid_keys']
    assert config.SYSTEM_BASE_ENVIRONMENT[key]==legacy and config.SYSTEM_ACTIVE_ENVIRONMENT[key]==legacy
    assert field(settings.public('owner'),key)['value']==replacement


def test_malformed_legacy_secret_is_masked_during_display_and_repair(settings,monkeypatch):
    legacy='synthetic legacy token containing spaces'
    replacement='repaired-synthetic-token'
    monkeypatch.setitem(config.SYSTEM_BASE_ENVIRONMENT,'BOT_TOKEN',legacy)
    monkeypatch.setitem(config.SYSTEM_ACTIVE_ENVIRONMENT,'BOT_TOKEN',legacy)
    current=settings.public('owner')
    assert field(current,'BOT_TOKEN')['configured']
    assert field(current,'BOT_TOKEN')['validation_error'] and 'BOT_TOKEN' in current['invalid_keys']
    assert 'value' not in field(current,'BOT_TOKEN') and 'active_value' not in field(current,'BOT_TOKEN')
    assert legacy not in json.dumps(current)
    with pytest.raises(ValueError):settings.save('owner',{'revision':current['revision'],'values':{'BOT_TOKEN':''}})
    assert not settings.store.path.exists()
    saved=settings.save('owner',{'revision':current['revision'],'values':{'BOT_TOKEN':replacement}})
    assert field(saved,'BOT_TOKEN')['restart_required']
    assert not field(saved,'BOT_TOKEN')['validation_error'] and field(saved,'BOT_TOKEN')['active_validation_error']
    assert legacy not in json.dumps(saved) and replacement not in json.dumps(saved)
    assert config.SYSTEM_ACTIVE_ENVIRONMENT['BOT_TOKEN']==legacy


def test_all_development_settings_are_editable_and_production_key_required(settings,monkeypatch):
    monkeypatch.setitem(config.SYSTEM_BASE_ENVIRONMENT,'SECRET_KEY','')
    with pytest.raises(ValueError):settings.save('owner',{'revision':'0','values':{'GATEBOT_ENV':'production'}})
    saved=settings.save('owner',{'revision':'0','values':{
        'GATEBOT_ENV':'production','SECRET_KEY':'new-production-key-with-at-least-32-chars',
        'DEV_REGISTRATION_SMS_REDIRECT_ENABLED':True,'DEV_REGISTRATION_SMS_PREFIX':'+155501',
        'DEV_REGISTRATION_SMS_PHONE':'+15550100200','GATEBOT_DATA_DIR':'./new-data','GATEBOT_LOG_DIR':'./new-logs'}})
    assert field(saved,'GATEBOT_ENV')['value']=='production'
    assert field(saved,'DEV_REGISTRATION_SMS_REDIRECT_ENABLED')['value'] is True


def test_tenant_cannot_read_or_mutate_and_missing_key_fails_closed(settings):
    for action in (lambda:settings.public('north'),lambda:settings.save('north',{'revision':'0','values':{'PORT':5555}})):
        with pytest.raises(PermissionError):action()
    assert not settings.store.directory.exists()
    settings.save('owner',{'revision':'0','values':{'PORT':5555}})
    settings.store.key_path.unlink()
    with pytest.raises(RuntimeError):settings.store.read()
    with pytest.raises(RuntimeError):settings.public('owner')


def test_corrupted_ciphertext_does_not_fall_back(settings):
    settings.save('owner',{'revision':'0','values':{'PORT':5555}})
    settings.store.path.write_bytes(b'invalid ciphertext')
    with pytest.raises(RuntimeError):settings.store.read()


def test_restart_loads_before_paths_secret_and_direct_environment_reads(tmp_path):
    # Load a copied config module whose project directory is entirely synthetic.
    project=tmp_path/'installation';(project/'storage').mkdir(parents=True)
    shutil.copyfile(Path(config.__file__),project/'storage'/'config.py')
    (project/'.env').write_text('GATEBOT_ENV=development\nPORT=5001\n',encoding='utf-8')
    store=SystemConfigStore(project_dir=project)
    store.save({'PORT':6789,'GATEBOT_DATA_DIR':str(project/'new-state'),
                'GATEBOT_ENV':'production','SECRET_KEY':'synthetic-new-session-key-with-32-characters',
                'YC_MARKETPLACE_APPLICATION_ID':'12345','DEV_REGISTRATION_SMS_REDIRECT_ENABLED':True},'0')
    code='''import importlib.util,json,os,sys
spec=importlib.util.spec_from_file_location("synthetic_config",sys.argv[1]);c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
print(json.dumps({"port":c.PORT,"data":c.DATA_DIR,"environment":c.ENVIRONMENT,"explicit_dev":c.EXPLICIT_DEVELOPMENT,"marketplace":c.YC_MARKETPLACE_APPLICATION_ID,"env_port":os.getenv("PORT"),"has_key":len(c.SECRET_KEY)>=32}))'''
    env=dict(os.environ);env['PORT']='5002'
    result=subprocess.run([sys.executable,'-X','utf8','-c',code,str(project/'storage'/'config.py')],env=env,capture_output=True,text=True,check=True)
    values=json.loads(result.stdout)
    assert values=={'port':6789,'data':str((project/'new-state').resolve()),'environment':'production','explicit_dev':False,'marketplace':'12345','env_port':'6789','has_key':True}
    assert not (project/'new-state').exists()


def test_catalog_covers_literal_runtime_environment_and_no_global_branch_tokens():
    root=Path(__file__).resolve().parents[1];names=set()
    for path in [*root.glob('*.py'),*(root/'storage').rglob('*.py')]:
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
            if isinstance(node,ast.Call) and node.args and isinstance(node.args[0],ast.Constant) and isinstance(node.args[0].value,str):
                function=node.func
                if (isinstance(function,ast.Attribute) and function.attr=='getenv') or (isinstance(function,ast.Name) and function.id in {'env_bool','env_int'}):names.add(node.args[0].value)
    assert names<=set(BY_KEY)|SMTP_ENV_KEYS|{'OUTBOUND_ENABLED'}  # compatibility with pre-split installations
    assert 'OUTBOUND_ENABLED' not in BY_KEY
    assert not {'YC_USER_TOKEN','YC_COMPANY_ID'}&set(BY_KEY)
