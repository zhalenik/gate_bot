"""Batch table metadata keeps single-client semantics and bounded document reads."""
import json

import pytest

from client_notification_admin import ClientNotificationAdmin
from storage.sql_documents import read_document, write_document
from test_identity_product import identities
from test_client_roles_web import roles_web
from test_web_security import web, login


def seed_clients(store, branch_id, count=100):
    tokens=[]
    with store.connect() as db:
        links=read_document(db,'links.json')
        for index in range(count):
            token='batch-client-'+str(index)
            tokens.append(token)
            links[token]={'branch_id':branch_id,'user_name':'Synthetic client '+str(index),
                          'phone':'+155502'+str(index).zfill(5),'mailing_enabled':index%3!=0,
                          'tg_id':str(73000+index) if index%2==0 else None,
                          'max_id':str(74000+index) if index%5==0 else None,
                          'email':'synthetic-'+str(index)+'@example.invalid',
                          'id_ycl':index+1 if index%4 else None,'is_temporary':index%11==0}
        write_document(db,'links.json',links)
        for index,token in enumerate(tokens):
            person=db.execute('SELECT person_id FROM branch_clients WHERE legacy_token=?',(token,)).fetchone()[0]
            preferences={'channels':{'email':True,'telegram':True,'max':True,'messengers':True}}
            if index%7==0:preferences['verified_email']=links[token]['email']
            db.execute('INSERT INTO notification_preferences(person_id,audience,branch_id,payload) VALUES(?,"client",?,?)',
                       (person,str(branch_id),json.dumps(preferences)))
    return tokens


def reads(statements,name):
    return sum(statement.startswith('SELECT payload FROM documents') and "'"+name+"'" in statement
               for statement in statements)


@pytest.mark.parametrize('crm',[True,False])
def test_hundred_clients_share_one_snapshot_and_match_single_results(identities,crm):
    s=identities;identity=s.service;notifications=ClientNotificationAdmin(identity)
    s.branches.update_settings('11',{'yclients_enabled':crm})
    if crm:identity.save_integration('owner','11','chatpush',{'token':'synthetic-token'})
    tokens=seed_clients(s.product,11)
    # Include branch administrator and superadministrator cards as well.
    tokens+=list(set(s.links.load_links())-set(tokens))
    expected_roles={token:identity.client_admin_state('owner',token) for token in tokens}
    expected_summaries={token:notifications.summary('owner',token) for token in tokens}
    statements=[]
    with s.product.connect() as db:
        db.set_trace_callback(statements.append)
        clients,accounts=identity._client_table_snapshot(db,'owner',tokens)
        roles=identity._client_admin_states(db,clients,accounts=accounts)
        summaries=notifications._summaries(db,clients)
        db.set_trace_callback(None)
    assert roles==expected_roles and summaries==expected_summaries
    assert reads(statements,'links.json')==1
    assert reads(statements,'admins.json')==1
    assert statements.count('SELECT username,person_id FROM web_accounts')==1
    assert statements.count('SELECT person_id,branch_id FROM branch_memberships WHERE status="active"')==1
    assert sum(role['is_super'] for role in roles.values())==1
    assert sum(role['last_admin'] for role in roles.values())==1
    assert summaries['batch-client-0']['reason']=='guest'
    assert summaries['batch-client-2']['effective_channels']['telegram']
    assert ('messengers' in summaries['batch-client-2']['allowed_channels'])==crm
    assert not s.sent


def test_batch_scope_rejects_historical_membership_and_revoked_actor(identities):
    s=identities;identity=s.service;notifications=ClientNotificationAdmin(identity)
    foreign=s.links.create_link({'branch_id':12,'phone':'+15550100666','user_name':'Other branch'})
    person=identity.web_admin('north')['person_id']
    with s.product.connect() as db:
        db.execute('INSERT INTO branch_memberships(person_id,branch_id,status,created) VALUES(?,"12","active",0)',(person,))
    for batch in (identity.client_admin_states,notifications.summaries):
        assert batch('north',['example-client'])
        with pytest.raises(PermissionError):batch('north',['example-client',foreign])
        with pytest.raises(ValueError):batch('owner',['missing-client'])
    with s.product.connect() as db:
        db.execute('UPDATE branch_memberships SET status="revoked" WHERE person_id=? AND branch_id="11"',(person,))
    for batch in (identity.client_admin_states,notifications.summaries):
        with pytest.raises(PermissionError):batch('north',['example-client'])
    assert not s.sent


def test_users_endpoint_keeps_document_reads_bounded_with_hundred_clients(roles_web):
    web,identity=roles_web
    seed_clients(identity.product,12)
    client=web.app.test_client();login(client,'tenant-a')
    statements=[]
    with identity.product.connect() as db:
        db.set_trace_callback(statements.append)
        response=client.get('/admin/api/users')
        db.set_trace_callback(None)
    assert response.status_code==200
    users=response.json['users']
    assert len(users)>=101 and all(str(user['branch_id'])=='12' for user in users)
    assert all('admin_role' in user and 'notification_status' in user for user in users)
    assert reads(statements,'links.json')<=4
    assert statements.count('SELECT username,person_id FROM web_accounts')==1
    assert not web.calls
