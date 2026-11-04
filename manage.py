"""Explicit local maintenance commands; never run them as a scheduled import."""
import argparse
import json
from pathlib import Path
from storage import config


def main():
    parser = argparse.ArgumentParser(description='GateBot local maintenance')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor', help='Inspect local state/configuration without network or modem I/O')
    sub.add_parser('init-demo', help='Create a fictional branch and pass for dry-run testing')
    support = sub.add_parser('support-bundle')
    support.add_argument('--output', default='support-bundle.zip')
    support.add_argument('--branch', type=int)
    sync = sub.add_parser('sync-once', help='Read YCLIENTS and update local passes once')
    sms = sub.add_parser('read-sms', help='Read the configured modem without deleting messages')
    sms.add_argument('--output', required=True, help='Private JSON output path; never commit it')
    args = parser.parse_args()
    if args.command == 'doctor':
        from diagnostics import doctor
        print(json.dumps(doctor(), ensure_ascii=False, indent=2))
    elif args.command == 'support-bundle':
        from diagnostics import support_bundle
        print(support_bundle(args.output, args.branch))
    elif args.command == 'init-demo':
        from branch_manager import BranchManager
        from link_manager import LinkManager
        if not config.GATE_DRY_RUN:
            raise SystemExit('Set GATE_DRY_RUN=true before creating a demo.')
        branch = json.loads((Path(__file__).parent / 'examples/branches.example.json').read_text(encoding='utf-8'))['1001']
        bm = BranchManager()
        if bm.get_branch(1001):
            raise SystemExit('Demo branch 1001 already exists; nothing changed.')
        bm.add_branch(branch)
        token = LinkManager().create_link({'branch_id': 1001, 'user_name': 'Демонстрационный клиент', 'phone': '', 'gate_access': True, 'mailing_enabled': False}, expires_days=1)
        print('Demo only; no real phone numbers or CRM data are used.')
        print(config.BASE_URL + '/opn/' + token)
    elif args.command == 'sync-once':
        from filelock import FileLock, Timeout
        from scheduler import Scheduler
        if not config.YCLIENTS_ENABLED:
            raise SystemExit('YCLIENTS_ENABLED is false. No requests made.')
        try:
            with FileLock(str(Path(config.DATA_DIR) / 'runtime.lock'), timeout=0):
                print(json.dumps(Scheduler().run_once(), ensure_ascii=False, indent=2))
        except Timeout:
            raise SystemExit('Stop the running service before manual synchronization.') from None
    elif args.command == 'read-sms':
        from filelock import FileLock, Timeout
        import sim800c
        try:
            with FileLock(str(Path(config.DATA_DIR) / 'runtime.lock'), timeout=0):
                messages = sim800c.read_sms()
                path = Path(args.output)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding='utf-8')
                print('SMS data saved to a private file. No messages were deleted.')
        except Timeout:
            raise SystemExit('Stop the running service before reading the modem separately.') from None
        finally:
            sim800c.shutdown()


if __name__ == '__main__':
    main()
