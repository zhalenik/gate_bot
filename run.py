"""Windows/Linux entry point: one owner of scheduler, bot delivery and COM port."""
from pathlib import Path
import os
import signal
import sys
import threading
from filelock import FileLock, Timeout
from storage import config
import logger

_unfinished_runtime_locks = []

def main():
    from link_manager import set_notify_hook
    from scheduler import Scheduler
    from waitress import create_server
    import sim800c

    lock = FileLock(str(Path(config.DATA_DIR) / 'runtime.lock'), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        raise SystemExit('GateBot is already running for this data directory. Stop the other instance first.')
    try:
        from storage.sql_documents import migrate_legacy
        migration = migrate_legacy(config.DATA_DIR, activate=True)
        from app import app, link_manager, branch_manager, access_service, admin_auth, product_services
        products = product_services()
        products.attach(access_service)
        products.billing.recover()
        products.index_clients()
    except BaseException:
        lock.release()
        raise
    stop = threading.Event()
    restart = threading.Event()
    threads = []
    service = access_service
    from yclients_api import YClientsClient
    crm = YClientsClient(branches=branch_manager,
                         credential_provider=lambda branch: products.identity.integration_credentials(branch, 'yclients')) if config.YCLIENTS_ENABLED else None
    scheduler = Scheduler(link_manager, branch_manager, crm=crm, event_sink=products.notifications.event)
    server = None
    dispatcher = None

    def on_signal(*_):
        restart.clear()
        service.stop_accepting()
        stop.set()

    def request_restart():
        if not stop.is_set():
            restart.set()
            service.stop_accepting()
            stop.set()

    def bot_worker(module, name):
        from bot_diagnostics import error_details
        from bot_webhooks import BotInbox,EXTENSION
        pause=2
        while not stop.is_set():
            try:
                try:
                    inbox=BotInbox.configured(name)
                except ValueError:
                    from bot_diagnostics import BotCheckError
                    raise BotCheckError('invalid_configuration',stage='configuration') from None
                if inbox.url:
                    app.extensions[EXTENSION][name]=inbox
                module.run_delivery(stop_event=stop, service=service, inbox=inbox)
                return
            except Exception as error:
                diagnosis = error_details(name,error)
                logger.event('BOT_STOPPED',level='ERROR',details={'platform':name,'error_type':type(error).__name__,**diagnosis})
                print(f"{name}: {diagnosis['stage']} / {diagnosis['error_code']}. {diagnosis['action']}",flush=True)
                # Retry startup only after a temporary provider/network failure;
                # the next attempt reads the live subscription before changing it.
                if (diagnosis['http_status'] not in {429,500,502,503,504} and diagnosis['error_code'] not in
                    {'connect_timeout','read_timeout','timeout','dns_error','proxy_error','connection_error',
                     'network_error','rate_limited','provider_unavailable'}):
                    return
            finally:
                app.extensions[EXTENSION].pop(name,None)
            stop.wait(pause)
            pause=min(60,pause*2)

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, on_signal)
    try:
        from telephony_store import TelephonyStore
        from bot_webhooks import EXTENSION
        if config.BOT_ENABLED or config.MAX_BOT_ENABLED:
            app.extensions[EXTENSION]={}
        TelephonyStore().recover_outgoing()
        if config.BOT_ENABLED:
            import telegram_bot
            threads.append(threading.Thread(target=bot_worker, args=(telegram_bot, 'telegram'), name='gatebot-telegram', daemon=True))
        if config.MAX_BOT_ENABLED:
            import max_bot
            threads.append(threading.Thread(target=bot_worker, args=(max_bot, 'max'), name='gatebot-max', daemon=True))
        dispatcher = products.notifications
        dispatcher._menu_refreshers = [__import__('telegram_bot').refresh_menus, __import__('max_bot').refresh_menus]
        hardware = sim800c._get_queue()
        dispatcher.hardware = hardware
        hardware.command_guard = lambda command_id: service.authorize_command(command_id, start=True)
        hardware.command_observer = service.command_finished
        set_notify_hook(dispatcher.access_transition)
        threads.append(threading.Thread(target=sim800c.monitor_status, args=(stop,),
                                        name='gatebot-sim800-status', daemon=True))
        dispatcher.start()
        # Bound locally by default. No Flask debugger or reloader, even on Windows.
        server = create_server(app, host=config.HOST, port=config.PORT, threads=4, clear_untrusted_proxy_headers=True)
        web = threading.Thread(target=server.run, name='gatebot-web', daemon=True)
        web.start()
        for thread in threads:
            thread.start()
        scheduler.start()
        products.yclients_client_jobs.start()
        import runtime_control
        runtime_control.bind_restart(request_restart)
        print(f'GateBot: {config.BASE_URL} | gate mode: {"DRY RUN" if config.GATE_DRY_RUN else "HARDWARE"}')
        logger.event('RUNTIME_STARTED', details={'dry_run': config.GATE_DRY_RUN, 'version': '0.2.0-alpha.25'})
        while not stop.wait(0.5):
            if not web.is_alive():
                raise RuntimeError('Web server stopped')
    except KeyboardInterrupt:
        stop.set()
    finally:
        from runtime_control import bind_restart
        bind_restart(None)
        # Web and both bots share this same AccessService. Refuse new physical
        # effects before waiting for background threads or closing the COM port.
        service.stop_accepting()
        stop.set()
        shutdown_errors = []
        if dispatcher:
            dispatcher.request_stop()

        def finish(component, action):
            try:
                return action()
            except Exception as error:
                shutdown_errors.append(component)
                try:
                    logger.event('SHUTDOWN_ERROR', level='ERROR', details={
                        'component': component, 'error_type': type(error).__name__})
                except Exception:
                    print(f'Shutdown error: {component} ({type(error).__name__}).')
                return False

        if server:
            finish('web', server.close)
        # Cancel accepted-but-not-yet-dialed requests immediately; a slow CRM
        # shutdown must not leave the physical queue running for another minute.
        if not finish('sim800', lambda: sim800c.shutdown(timeout=10)):
            shutdown_errors.append('sim800_unconfirmed')
        dispatcher_pool = getattr(server, 'task_dispatcher', None)
        if dispatcher_pool:
            finish('web_requests', lambda: dispatcher_pool.shutdown(timeout=35))
            if dispatcher_pool.threads:
                shutdown_errors.append('web_requests_unconfirmed')
        finish('scheduler', scheduler.stop)
        if finish('yclients_client_ids', products.yclients_client_jobs.stop) is False:
            shutdown_errors.append('yclients_client_ids_unconfirmed')
        if dispatcher:
            if finish('notifications', dispatcher.stop) is False:
                shutdown_errors.append('notifications_unconfirmed')
        set_notify_hook(None)
        for thread in threads:
            if thread.ident is not None:
                finish(thread.name, lambda worker=thread: worker.join(timeout=35))
        alive = [worker.name for worker in threads if worker.is_alive()]
        alive.extend(worker.name for worker in (getattr(scheduler, '_thread', None),
                     getattr(scheduler, '_activation_thread', None),
                     getattr(products.yclients_client_jobs, '_thread', None)) if worker and worker.is_alive())
        if dispatcher:
            alive.extend(worker.name for worker in (getattr(dispatcher, 'thread', None),
                         getattr(dispatcher, 'menu_thread', None)) if worker and worker.is_alive())
        try:
            logger.event('RUNTIME_STOPPED', level='WARNING' if shutdown_errors or alive else 'INFO',
                         details={'clean_shutdown': not shutdown_errors and not alive,
                                  'shutdown_errors': shutdown_errors, 'workers_still_alive': alive})
        finally:
            if alive or shutdown_errors:
                # Retain ownership until this process exits if a worker has not
                # stopped. A new runtime must not overlap uncertain effects.
                _unfinished_runtime_locks.append(lock)
            else:
                lock.release()
    if restart.is_set() and not shutdown_errors and not alive:
        os.execve(sys.executable, [sys.executable, str(Path(__file__).resolve())], config.PROCESS_ENVIRONMENT)


if __name__ == '__main__':
    main()
