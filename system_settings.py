"""Super-only settings editor. Saving does not reconfigure a running runtime."""
from storage import config
from system_config_bootstrap import SystemConfigStore
from system_settings_catalog import BY_KEY, FIELDS, GROUPS, decode, default_values, encode, validate_effective


def _inspect_value(descriptor, value):
    """Reading a legacy environment must not prevent fixing it in the editor."""
    try:
        normalized=encode(descriptor,value)
        return normalized,decode(descriptor,normalized),None
    except ValueError as error:
        # Validation messages contain only field names/rules, never their values.
        return value,value,str(error)


class SystemSettings:
    def __init__(self,identity,store=None):
        self.identity=identity
        self.store=store or SystemConfigStore(project_dir=config.PROJECT_DIR)

    def _super(self,username):
        if self.identity.web_admin(username).get('is_super') is not True:
            raise PermissionError('Нет доступа к системным настройкам')

    def public(self,username):
        self._super(username)
        saved=self.store.read();target=default_values({**config.SYSTEM_BASE_ENVIRONMENT,**saved['values']})
        running=default_values(config.SYSTEM_ACTIVE_ENVIRONMENT)
        fields=[]
        for descriptor in FIELDS:
            key=descriptor['key']
            normalized,value,error=_inspect_value(descriptor,target[key])
            active_normalized,active_value,active_error=_inspect_value(descriptor,running[key])
            changed=normalized!=active_normalized
            field={**descriptor,'source':'saved' if key in saved['values'] else ('environment' if key in config.SYSTEM_BASE_ENVIRONMENT else 'default'),
                   'overridden':key in saved['values'],'restart_required':changed,
                   'validation_error':error,'active_validation_error':active_error}
            if descriptor['secret']:
                field.update(configured=bool(target[key]),active_configured=bool(running[key]))
                field.pop('default',None)
            else:
                field.update(value=value,active_value=active_value)
            fields.append(field)
        return {'revision':saved['revision'],'groups':GROUPS,'fields':fields,
                'restart_required':any(field['restart_required'] for field in fields),
                'restart_keys':[field['key'] for field in fields if field['restart_required']],
                'invalid_keys':[field['key'] for field in fields if field['validation_error']],
                'active_invalid_keys':[field['key'] for field in fields if field['active_validation_error']]}

    def save(self,username,payload):
        self._super(username)
        if not isinstance(payload,dict) or set(payload)-{'revision','values','reset','clear'}:
            raise ValueError('Некорректные системные настройки')
        updates=payload.get('values',{});reset=payload.get('reset',[]);clear=payload.get('clear',[])
        if not isinstance(updates,dict) or any(not isinstance(items,list) or any(not isinstance(key,str) for key in items) for items in (reset,clear)):
            raise ValueError('Некорректные изменения настроек')
        keys=set(updates)|set(reset)|set(clear)
        if keys-set(BY_KEY) or set(reset)&(set(updates)|set(clear)) or set(clear)&set(updates):
            raise ValueError('Неизвестные или противоречивые параметры')
        if any(not BY_KEY[key]['secret'] for key in clear):raise ValueError('Удаление секрета применимо только к секретным полям')
        saved=self.store.read()
        if payload.get('revision')!=saved['revision']:raise ValueError('Настройки уже изменились. Обновите страницу')
        values=dict(saved['values'])
        for key in reset:values.pop(key,None)
        for key in clear:values[key]=''
        for key,value in updates.items():
            # Empty password fields mean retain; clearing requires the explicit action.
            if BY_KEY[key]['secret'] and value=='':continue
            values[key]=encode(BY_KEY[key],value)
        effective=default_values({**config.SYSTEM_BASE_ENVIRONMENT,**values})
        validate_effective(effective)
        # Check authorization again immediately before the private mutation.
        with self.identity.product.connect():
            self._super(username)
            self.store.save(values,saved['revision'])
        import logger
        logger.event('SYSTEM_SETTINGS_CHANGED',actor=username,details={'keys':sorted(keys),'restart_required':True})
        return self.public(username)
