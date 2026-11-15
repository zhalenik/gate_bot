"""Windows/Linux entry point: one owner of scheduler, bot polling and COM port."""
from pathlib import Path
import signal
import threading
from filelock import FileLock, Timeout
from storage import config
import logger


def main():
    from app import app, link_manager, branch_manager, access_service
    from link_manager import set_notify_hook
    from scheduler import Scheduler
    from notifications import NotificationDispatcher
    from waitress import create_server
    import sim800c

    lock = FileLock(str(Path(config.DATA_DIR) / 'runtime.lock'), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        raise SystemExit('GateBot is already running for this data directory. Stop the other instance first.')
    stop = threading.Event()
    threads = []
    callbacks = {}
    ready = {}
    service = access_service
    scheduler = Scheduler(link_manager, branch_manager)
    server = None
    dispatcher = None

    def on_signal(*_):
        service.stop_accepting()
        stop.set()

    def bot_worker(module, name):
        try:
            module.run_polling(stop_event=stop, service=service)
        except Exception as error:
            # Never loop-restart a bot that may own an external delivery cursor.
            from bot_diagnostics import error_details
            diagnosis = error_details(name, error)
            logger.event('BOT_STOPPED', level='ERROR', details={
                'platform': name, 'error_type': type(error).__name__,
                **diagnosis})
            print(f"{name}: stopped; code={diagnosis['error_code']}; "
                  f"stage={diagnosis['stage']}; HTTP={diagnosis['http_status'] or '-'}.", flush=True)
            print(diagnosis['action'], flush=True)
            print(f'Check: python manage.py check-bots --platform {name}', flush=True)

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, on_signal)
    try:
        if config.BOT_ENABLED:
            if config.BOT_MODE != 'polling' or not config.BOT_TOKEN:
                raise ValueError('This test build requires BOT_MODE=polling and BOT_TOKEN')
            import telegram_bot
            callbacks['telegram'] = telegram_bot.notify_access
            ready['telegram'] = telegram_bot.status
            threads.append(threading.Thread(target=bot_worker, args=(telegram_bot, 'telegram'), name='gatebot-telegram', daemon=True))
        if config.MAX_BOT_ENABLED:
            if config.MAX_BOT_MODE != 'polling' or not config.MAX_BOT_TOKEN:
                raise ValueError('This test build requires MAX_BOT_MODE=polling and MAX_BOT_TOKEN')
            import max_bot
            callbacks['max'] = max_bot.notify_access
            ready['max'] = max_bot.status
            threads.append(threading.Thread(target=bot_worker, args=(max_bot, 'max'), name='gatebot-max', daemon=True))
        dispatcher = NotificationDispatcher(callbacks, ready=ready, links=link_manager)
        set_notify_hook(dispatcher.enqueue)
        for thread in threads:
            thread.start()
        dispatcher.start()
        scheduler.start()
        # Bound locally by default. No Flask debugger or reloader, even on Windows.
        server = create_server(app, host=config.HOST, port=config.PORT, threads=4, clear_untrusted_proxy_headers=True)
        web = threading.Thread(target=server.run, name='gatebot-web', daemon=True)
        web.start()
        print(f'GateBot: {config.BASE_URL} | gate mode: {"DRY RUN" if config.GATE_DRY_RUN else "HARDWARE"}')
        logger.event('RUNTIME_STARTED', details={'dry_run': config.GATE_DRY_RUN, 'version': '0.1.0-alpha.1'})
        while not stop.wait(0.5):
            if not web.is_alive():
                raise RuntimeError('Web server stopped')
    except KeyboardInterrupt:
        stop.set()
    finally:
        # Web and both bots share this same AccessService. Refuse new physical
        # effects before waiting for background threads or closing the COM port.
        service.stop_accepting()
        stop.set()
        shutdown_errors = []

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
        finish('scheduler', scheduler.stop)
        if dispatcher:
            finish('notifications', dispatcher.stop)
        set_notify_hook(None)
        for thread in threads:
            finish(thread.name, lambda worker=thread: worker.join(timeout=35))
        alive = [worker.name for worker in threads if worker.is_alive()]
        try:
            logger.event('RUNTIME_STOPPED', level='WARNING' if shutdown_errors or alive else 'INFO',
                         details={'clean_shutdown': not shutdown_errors and not alive,
                                  'shutdown_errors': shutdown_errors, 'workers_still_alive': alive})
        finally:
            lock.release()


if __name__ == '__main__':
    main()
