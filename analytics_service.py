"""Tenant-scoped analytics over durable requests, never rotated diagnostic logs."""
from collections import defaultdict
from datetime import datetime, timezone, timedelta
import csv
import io
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from billing_service import BillingService, epoch, iso, identifier


DIMENSIONS = {'branch','person','client','gate','role','pass_type','source','status','hour','day','week','month'}


def selected(values, label):
    if values is None:return None
    if not isinstance(values,list) or len(values)>1000:raise ValueError('Некорректный фильтр: '+label)
    return [identifier(v,label) for v in values]


class AnalyticsService:
    def __init__(self, store=None):
        self.billing=BillingService(store)
        self.store=self.billing.store

    def record_client(self, branch_client_id, branch_id, person_id, *, created_at=None, source='local', role='client', db=None):
        with self.billing._transaction(db) as tx:
            tx.execute('INSERT OR IGNORE INTO client_branch_events VALUES(?,?,?,?,?,?)',(str(branch_client_id),str(branch_id),str(person_id),epoch(created_at),source,role))

    @staticmethod
    def _filter(filters, allowed_branches):
        if not isinstance(filters,dict):raise ValueError('Ожидается объект фильтров')
        requested=selected(filters.get('branches'),'филиалы')
        if allowed_branches is not None:
            allowed={str(x) for x in allowed_branches}
            if requested and not set(requested)<=allowed:raise PermissionError('Нет доступа к выбранному филиалу')
            branches=requested if requested is not None else sorted(allowed)
        else:branches=requested
        end=epoch(filters.get('to'))
        start=epoch(filters.get('from',end-30*86400))
        if start>=end:raise ValueError('Начало периода должно быть раньше окончания')
        try:zone=ZoneInfo(filters.get('timezone','Europe/Moscow'))
        except (ZoneInfoNotFoundError,TypeError):raise ValueError('Неизвестный часовой пояс') from None
        grouping=filters.get('group_by',['day'])
        if not isinstance(grouping,list) or not 1<=len(grouping)<=3 or not all(isinstance(d,str) for d in grouping) or not set(grouping)<=DIMENSIONS or len(grouping)!=len(set(grouping)):
            raise ValueError('Выберите от одной до трёх разных группировок')
        clauses=['happened_at>=?','happened_at<?'];args=[start,end]
        fields={'branches':('branch_id',branches),'people':('person_id',selected(filters.get('people'),'люди')),
            'clients':('branch_client_id',selected(filters.get('clients'),'клиенты')),
            'gates':('gate_id',selected(filters.get('gates'),'ворота')),
            'roles':('client_role',selected(filters.get('roles'),'роли')),
            'pass_types':('pass_type',selected(filters.get('pass_types'),'типы пропуска')),
            'sources':('source',selected(filters.get('sources'),'каналы')),
            'statuses':('status',selected(filters.get('statuses'),'результаты'))}
        for column,values in fields.values():
            if values is not None:
                clauses.append(column+' IN ('+','.join('?' for _ in values)+')' if values else '0')
                args.extend(values)
        if not filters.get('include_simulated',False):clauses.append('status<>"simulated"')
        return start,end,zone,grouping,clauses,args,fields

    @staticmethod
    def _key(row, dimension, zone):
        mapping={'branch':'branch_id','person':'person_id','client':'branch_client_id','gate':'gate_id','role':'client_role','pass_type':'pass_type','source':'source','status':'status'}
        if dimension in mapping:return row[mapping[dimension]] or 'unknown'
        local=datetime.fromtimestamp(row['happened_at'],zone)
        if dimension=='hour':return local.replace(minute=0,second=0,microsecond=0).isoformat()
        if dimension=='day':return local.date().isoformat()
        if dimension=='month':return local.strftime('%Y-%m')
        return (local.date()-timedelta(days=local.weekday())).isoformat()

    def query(self, filters, *, allowed_branches):
        start,end,zone,grouping,clauses,args,fields=self._filter(filters,allowed_branches)
        groups={};total=0;statuses=defaultdict(int);commands=set();people=set();active_days=set();coalesced=0
        with self.store.connect() as db:
            for row in db.execute('SELECT * FROM gate_requests WHERE '+' AND '.join(clauses)+' ORDER BY happened_at,id',args):
                total+=1;statuses[row['status']]+=1;people.add(row['person_id'])
                if row['command_id']:commands.add(row['command_id'])
                active_days.add(datetime.fromtimestamp(row['happened_at'],zone).date())
                coalesced+=row['coalesced']
                key=tuple(self._key(row,d,zone) for d in grouping)
                group=groups.setdefault(key,{'requests':0,'commands':set(),'people':set(),'denied':0,'charged_commands':set()})
                group['requests']+=1;group['people'].add(row['person_id'])
                if row['command_id']:group['commands'].add(row['command_id'])
                if row['status']=='denied':group['denied']+=1
            charged=set()
            if commands:
                for row in db.execute('SELECT id FROM hardware_commands WHERE charged=1'):
                    if row['id'] in commands:charged.add(row['id'])
            newclauses=['created>=?','created<?'];newargs=[start,end]
            for filter_key,column in [('branches','branch_id'),('people','person_id'),('clients','branch_client_id'),('roles','client_role')]:
                values=fields[filter_key][1]
                if values is not None:
                    newclauses.append(column+' IN ('+','.join('?' for _ in values)+')' if values else '0');newargs.extend(values)
            newrows=db.execute('SELECT COUNT(*) clients,COUNT(DISTINCT person_id) people FROM client_branch_events WHERE '+' AND '.join(newclauses),newargs).fetchone()
            # Reuse every visibility filter, omitting only the report's time range.
            first=db.execute('SELECT MIN(happened_at) first FROM gate_requests WHERE '+' AND '.join(clauses[2:] or ['1']),args[2:]).fetchone()['first']
        hours=(end-start)/3600;days=hours/24
        output=[]
        for key,group in sorted(groups.items()):
            output.append({'dimensions':dict(zip(grouping,key)),'requests':group['requests'],'commands':len(group['commands']),
                'charged_commands':len(group['commands']&charged),'unique_people':len(group['people']),'denied':group['denied']})
        # Fill time-only day/hour/month buckets, including zero-activity intervals.
        if len(grouping)==1 and grouping[0] in {'day','hour','month','week'}:
            dimension=grouping[0];known={r['dimensions'][dimension]:r for r in output}
            cursor=datetime.fromtimestamp(start,zone)
            cursor=cursor.replace(minute=0,second=0,microsecond=0) if dimension=='hour' else cursor.replace(hour=0,minute=0,second=0,microsecond=0)
            if dimension=='month':cursor=cursor.replace(day=1)
            if dimension=='week':cursor-=timedelta(days=cursor.weekday())
            # UTC stepping retains both repeated local hours across DST fallback.
            count=0
            while cursor.timestamp()<end:
                key=self._key({'happened_at':cursor.timestamp()},dimension,zone)
                known.setdefault(key,{'dimensions':{dimension:key},'requests':0,'commands':0,'charged_commands':0,'unique_people':0,'denied':0})
                count+=1
                if count>20000:raise ValueError('Для этого периода выберите более крупную группировку')
                if dimension=='month':
                    y,m=divmod(cursor.year*12+cursor.month,12);cursor=cursor.replace(year=y,month=m+1,day=1,hour=0,minute=0,second=0,microsecond=0)
                elif dimension=='hour':cursor=datetime.fromtimestamp(cursor.timestamp()+3600,zone)
                else:cursor+=timedelta(days=7 if dimension=='week' else 1)
            output=[known[k] for k in sorted(known)]
        if len(output)>20000:raise ValueError('Слишком много групп: сузьте выборку')
        result={'from':iso(start),'to':iso(end),'timezone':str(zone),'group_by':grouping,'groups':output,
            'summary':{'requests':total,'commands':len(commands),'charged_commands':len(charged),'coalesced_requests':coalesced,
                'unique_people':len(people),'new_branch_clients':newrows['clients'],'new_people':newrows['people'],
                'average_per_hour':round(total/hours,6),'average_per_day':round(total/days,6),
                'average_commands_per_hour':round(len(charged)/hours,6),'average_commands_per_day':round(len(charged)/days,6),
                'average_per_active_day':round(total/len(active_days),6) if active_days else 0,'statuses':dict(statuses)},
            'coverage_from':iso(first),'notes':['Физическое положение ворот не измеряется.','Новые клиенты считаются по филиалам и людям; фильтры ворот, канала и результата к этой метрике не применяются.','Команды, общие для нескольких клиентов, могут входить в несколько групп; общий итог считает их один раз.']}
        if filters.get('compare_previous') is True:
            previous=self.query({**filters,'from':start-(end-start),'to':start,'compare_previous':False},allowed_branches=allowed_branches)
            result['comparison']={'from':previous['from'],'to':previous['to'],'summary':previous['summary'],
                'changes':{key:result['summary'][key]-previous['summary'][key] for key in ('requests','commands','charged_commands','new_branch_clients','unique_people')}}
        return result

    def records(self, filters, *, allowed_branches, page=1):
        *_,clauses,args,fields=self._filter(filters,allowed_branches)
        if type(page) is not int or page<1:raise ValueError('Некорректная страница')
        with self.store.connect() as db:
            total=db.execute('SELECT COUNT(*) n FROM gate_requests WHERE '+' AND '.join(clauses),args).fetchone()['n']
            rows=[dict(r) for r in db.execute('SELECT * FROM gate_requests WHERE '+' AND '.join(clauses)+' ORDER BY happened_at DESC,id LIMIT 100 OFFSET ?',args+[(page-1)*100])]
        return {'records':rows,'total':total,'page':page}

    @staticmethod
    def csv(result):
        output=io.StringIO(newline='');writer=csv.writer(output)
        dimensions=result['group_by'];metrics=['requests','commands','charged_commands','unique_people','denied']
        writer.writerow(dimensions+metrics)
        for row in result['groups']:
            values=[str(row['dimensions'][k]) for k in dimensions]+[row[k] for k in metrics]
            writer.writerow(["'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v for v in values])
        return '\ufeff'+output.getvalue()
