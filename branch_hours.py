"""Weekly branch access overlay; never mutates grants or schedules messages."""
from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo

from storage.sql_documents import read_document, write_document


CLOSED_MESSAGE = 'Извините, доступ возможен только в рабочие часы. Вы можете связаться с администратором.'


def normalize_schedule(value):
    if value is None:
        return {'enabled': False, 'days': {str(day): [] for day in range(7)}}
    if not isinstance(value, dict) or set(value)-{'enabled','days'} or type(value.get('enabled')) is not bool:
        raise ValueError('Некорректное расписание доступа')
    days=value.get('days',{})
    if not isinstance(days,dict) or set(days)-{str(day) for day in range(7)}:
        raise ValueError('Укажите расписание по дням недели')
    normalized={}
    for day in range(7):
        windows=days.get(str(day),[])
        if not isinstance(windows,list) or len(windows)>4:
            raise ValueError('Допустимо до четырёх интервалов в день')
        normalized[str(day)]=[]
        for window in windows:
            if not isinstance(window,dict) or set(window)!={'start','end'}:
                raise ValueError('Укажите начало и конец интервала')
            for key in ('start','end'):
                text=window[key]
                if not isinstance(text,str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',text):
                    if not (key=='end' and text=='24:00'):
                        raise ValueError('Время должно иметь формат ЧЧ:ММ; 24:00 допустимо только в конце интервала')
            if window['start']==window['end']:
                raise ValueError('Начало и конец интервала совпадают. Для всего дня укажите 00:00–24:00')
            normalized[str(day)].append(dict(window))
        normalized[str(day)].sort(key=lambda window:window['start'])
    return {'enabled':value['enabled'],'days':normalized}


def _minutes(text):
    hour,minute=map(int,text.split(':'))
    return hour*60+minute


def _open_at(schedule,instant,zone):
    local=instant.astimezone(zone);minute=local.hour*60+local.minute
    for window in schedule['days'][str(local.weekday())]:
        start,end=_minutes(window['start']),_minutes(window['end'])
        if (start<=minute<end if end>start else minute>=start):return True
    for window in schedule['days'][str((local.weekday()-1)%7)]:
        start,end=_minutes(window['start']),_minutes(window['end'])
        if end<start and minute<end:return True
    return False


def _next_change(schedule,instant,zone):
    candidates=set();local=instant.astimezone(zone)
    # Local wall-clock boundaries are checked in both DST folds. Nonexistent
    # times are ignored; zone transition candidates cover skipped boundaries.
    for offset in range(-1,9):
        date=(local+timedelta(days=offset)).date()
        for window in schedule['days'][str(date.weekday())]:
            start,end=_minutes(window['start']),_minutes(window['end'])
            for minute in (start,end+(1440 if end<start else 0)):
                naive=datetime.combine(date,datetime.min.time())+timedelta(minutes=minute)
                for fold in (0,1):
                    candidate=naive.replace(tzinfo=zone,fold=fold).astimezone(timezone.utc)
                    # For a skipped time, the mapped boundary still gives the
                    # correct first instant after the clock jump when checked.
                    if candidate>instant:candidates.add(candidate)
    # An offset transition may itself cross a missing/repeated opening boundary.
    cursor=instant.replace(second=0,microsecond=0)+timedelta(minutes=1)
    until=instant+timedelta(days=8)
    while cursor<until:
        previous=cursor-timedelta(hours=1)
        if cursor.astimezone(zone).utcoffset()!=previous.astimezone(zone).utcoffset():
            probe=previous.replace(second=0,microsecond=0)
            for _ in range(61):
                if probe>instant:candidates.add(probe)
                probe+=timedelta(minutes=1)
        cursor+=timedelta(hours=1)
    for candidate in sorted(candidates):
        if _open_at(schedule,candidate-timedelta(microseconds=1),zone)!=_open_at(schedule,candidate,zone):
            return candidate.isoformat()
    return None


def schedule_state(schedule,zone_name='Europe/Moscow',at=None):
    instant=at if at is not None else datetime.now(timezone.utc)
    if isinstance(instant,(int,float)):instant=datetime.fromtimestamp(instant,timezone.utc)
    if instant.tzinfo is None:raise ValueError('Время проверки должно включать часовой пояс')
    try:
        value=normalize_schedule(schedule);zone=ZoneInfo(zone_name)
    except (ValueError,KeyError,TypeError):
        return {'enabled':True,'open':False,'message':CLOSED_MESSAGE,'next_change_at':None,'timezone':zone_name,'invalid':True}
    if not value['enabled']:
        return {'enabled':False,'open':True,'message':None,'next_change_at':None,'timezone':zone_name}
    opened=_open_at(value,instant,zone)
    return {'enabled':True,'open':opened,'message':None if opened else CLOSED_MESSAGE,
            'next_change_at':_next_change(value,instant,zone),'timezone':zone_name}


def access_hours_state(branches,branch_id,at=None):
    branch=branches.load_branches().get(str(branch_id))
    if not branch:
        return {'enabled':True,'open':False,'message':CLOSED_MESSAGE,'next_change_at':None,'timezone':None}
    return schedule_state(branch.get('access_schedule'),branch.get('timezone','Europe/Moscow'),at)


class BranchHoursService:
    def __init__(self,identity):self.identity=identity

    def read(self,username,branch_id):
        with self.identity.product.connect() as db:
            self.identity._scope(db,username,branch_id)
            branch=read_document(db,'branches.json')[str(branch_id)]
            return {'branch_id':str(branch_id),'timezone':branch.get('timezone','Europe/Moscow'),
                    'schedule':normalize_schedule(branch.get('access_schedule')),
                    'state':schedule_state(branch.get('access_schedule'),branch.get('timezone','Europe/Moscow'))}

    def save(self,username,branch_id,schedule):
        normalized=normalize_schedule(schedule)
        with self.identity.product.connect() as db:
            self.identity._scope(db,username,branch_id)
            branches=read_document(db,'branches.json')
            branches[str(branch_id)]['access_schedule']=normalized
            write_document(db,'branches.json',branches)
        import logger
        logger.event('BRANCH_ACCESS_HOURS_CHANGED',branch_id=branch_id,actor=username,details={'enabled':normalized['enabled']})
        return self.read(username,branch_id)
