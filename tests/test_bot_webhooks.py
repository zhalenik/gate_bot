"""Offline ingress, redelivery, registration and runtime mode transitions."""
import asyncio
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot_state import BotStateStore
from bot_webhooks import BotInbox,EXTENSION,HEADERS
from storage import config
from test_web_security import web


@pytest.mark.parametrize('platform',['telegram','max'])
def test_real_ingress_authenticates_persists_and_deduplicates(web,tmp_path,monkeypatch,platform):
    from test_max_bot import raw_message
    state=BotStateStore(tmp_path/'webhook.sqlite3')
    inbox=BotInbox(platform,'synthetic-private-bot',state,url='https://example.invalid/webhook/'+platform)
    monkeypatch.setitem(web.app.extensions,EXTENSION,{platform:inbox})
    raw=({'update_id':20,'message':{'text':'synthetic private text'}} if platform=='telegram'
         else raw_message(text='synthetic private text'))
    client=web.app.test_client()
    path='/webhook/'+platform
    assert client.post(path,json=raw).status_code==401
    headers={HEADERS[platform]:inbox.secret}
    for _ in range(2):
        assert client.post(path,json=raw,headers=headers).status_code==200
    with sqlite3.connect(state.path) as db:
        ciphertext,=db.execute('SELECT payload FROM webhook_inbox').fetchone()
        assert b'synthetic private text' not in ciphertext
    restarted=BotInbox(platform,'synthetic-private-bot',BotStateStore(state.path),url=inbox.url)
    assert restarted.take()==raw
    assert client.post(path,json=raw,headers=headers).status_code==200
    assert restarted.take() is None
    assert client.post(path,json={},headers=headers).status_code==400
    assert client.post('/admin/api/branch-settings',json={},headers=headers).status_code in (401,403)


def test_telegram_runtime_registers_processes_and_switches_back(tmp_path,monkeypatch):
    import telegram_bot
    from test_telegram_bot import fake_application,patch_runtime,Service
    application=fake_application()
    patch_runtime(monkeypatch,tmp_path,application)
    monkeypatch.setattr(config,'BOT_MODE','webhook')
    monkeypatch.setattr(config,'BASE_URL','https://example.invalid')
    monkeypatch.setattr(config,'WEBHOOK_URL','')
    monkeypatch.setattr(config,'TELEGRAM_WEBHOOK_SECRET','')
    inbox=BotInbox.configured('telegram')
    stop=threading.Event()
    application.bot.set_webhook=AsyncMock(return_value=True)
    application.bot.delete_webhook=AsyncMock(return_value=True)
    async def consume(update):
        assert update.update_id==123
        assert telegram_bot.status()=={'running':True,'ready':True,'mode':'webhook'}
        stop.set()
    application.process_update=AsyncMock(side_effect=consume)
    inbox.enqueue({'update_id':123})
    asyncio.run(asyncio.wait_for(telegram_bot._run(stop,Service(),inbox),timeout=5))
    application.process_update.assert_awaited_once()
    application.updater.start_polling.assert_not_awaited()
    opts=application.bot.set_webhook.await_args.kwargs
    assert opts['url']==inbox.url and opts['secret_token']==inbox.secret and not opts['drop_pending_updates']
    application.bot.get_webhook_info.return_value=SimpleNamespace(url=inbox.url)
    asyncio.run(telegram_bot._run(stop,Service(),inbox))
    assert application.bot.set_webhook.await_count==1
    monkeypatch.setattr(config,'BOT_MODE','polling')
    asyncio.run(telegram_bot._run(stop,Service(),BotInbox.configured('telegram')))
    application.bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
    assert not application.updater.start_polling.await_args.kwargs['drop_pending_updates']


def test_max_runtime_registers_delivers_and_switches_back(tmp_path,monkeypatch):
    from max_bot import MaxBot
    from test_max_bot import API,Service,raw_message
    api=API()
    api.get_me=Mock(return_value={})
    api.get_subscriptions=Mock(return_value=[])
    api.subscribe=Mock()
    api.unsubscribe=Mock()
    state=BotStateStore(tmp_path/'webhook.sqlite3')
    stop=threading.Event()
    bot=MaxBot(api,Service(),state,token='synthetic')
    url='https://example.invalid/webhook/max'
    inbox=BotInbox('max','synthetic',state,url=url)
    inbox.enqueue(raw_message())
    def send(**data):
        stop.set()
        return {'message':{'body':{'mid':'sent'}}}
    api.send_message=Mock(side_effect=send)
    bot.run(stop,inbox)
    api.subscribe.assert_called_once_with(url,inbox.secret)
    api.send_message.assert_called_once()
    assert not api.markers
    api.get_subscriptions.return_value=[{'url':url}]
    api.get_updates=lambda **_: (stop.set() or {'updates':[],'marker':1})
    stop.clear()
    bot.run(stop,BotInbox('max','synthetic',state))
    api.unsubscribe.assert_called_once_with(url)
    assert not state.get_checkpoint(inbox.namespace,'owned_webhook')


def test_webhook_diagnosis_and_max_wire_contract(monkeypatch):
    from bot_diagnostics import _subscription_report
    from max_api import MaxApiClient,MaxApiError,UPDATE_TYPES
    monkeypatch.setattr(config,'MAX_BOT_MODE','webhook')
    monkeypatch.setattr(config,'BASE_URL','https://example.invalid')
    url='https://example.invalid/webhook/max'
    assert _subscription_report('max',{},[url],'subscriptions')['status']=='ok'
    assert _subscription_report('max',{},[],'subscriptions')['error_code']=='webhook_missing'
    api=MaxApiClient('synthetic-token')
    api._request=Mock(return_value={'success':True})
    api.subscribe(url,'synthetic-secret')
    api._request.assert_called_once_with('POST','/subscriptions',body={'url':url,'secret':'synthetic-secret','update_types':list(UPDATE_TYPES)})
    api.unsubscribe(url)
    api._request.assert_called_with('DELETE','/subscriptions',params={'url':url})
    api._request.return_value={'success':False}
    with pytest.raises(MaxApiError):
        api.subscribe(url,'synthetic-secret')


@pytest.mark.parametrize('platform',['telegram','max'])
def test_existing_url_without_local_registration_is_not_owned(tmp_path,platform):
    from bot_diagnostics import BotCheckError
    state=BotStateStore(tmp_path/'bots.sqlite3')
    url='https://example.invalid/webhook/'+platform
    inbox=BotInbox(platform,'synthetic',state,url=url)
    with pytest.raises(BotCheckError):
        inbox.check_ownership([url])
    inbox.remember(inbox.registration(False))
    assert inbox.check_ownership([url])==inbox.registration(False)
    with pytest.raises(BotCheckError):
        inbox.check_ownership([url,'https://other.invalid/receiver'])


def test_max_event_keys_survive_delivery_and_mode_changes(tmp_path):
    from max_bot import MaxBot,normalize_update
    from test_max_bot import API,Service,NOW,raw_callback,raw_message
    state=BotStateStore(tmp_path/'bot.sqlite3')
    inbox=BotInbox('max','synthetic',state,url='https://example.invalid/webhook/max')
    bot=MaxBot(API(),Service(),state,token='synthetic',clock=lambda: NOW)
    msg=raw_message()
    inbox.enqueue(msg)
    msg['timestamp']+=5
    inbox.enqueue(msg)
    bot.process_update(inbox.take())
    assert inbox.take() is None
    bot.process_update(msg)
    assert len(bot.api.sent)==1
    callback=raw_callback()
    bot.process_update(callback)
    callback['timestamp']+=5
    callback['message']['body'].pop('mid')
    bot.process_update(callback)
    assert len([c for c in bot.service.calls if c[0]=='open'])==1
    distinct=raw_callback(callback_id='historical-key')
    state.claim(bot.namespace,normalize_update(distinct).previous_key)
    bot.process_update(distinct)
    assert len([c for c in bot.service.calls if c[0]=='open'])==1
