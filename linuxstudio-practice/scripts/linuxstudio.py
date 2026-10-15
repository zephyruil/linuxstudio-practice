"""Single JSON CLI. Passwords are entered only in the native local login dialog."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


def job_path(value):
    from runtime_paths import JOBS_DIR
    try:
        normalized = str(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError('Invalid job id.') from exc
    if normalized != value:
        raise ValueError('Use the exact job id returned by job start.')
    return JOBS_DIR / normalized


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def validated_answers(report, answers):
    if report.get('status') != 'awaiting_answers':
        raise ValueError('Job is not awaiting answers. Read job status first.')
    allowed = {p['key'] for p in report['pending']}
    if (not isinstance(answers, dict) or not answers or
        not set(answers).issubset(allowed) or
        any(not isinstance(a, str) or not a.strip() or len(a) > 2000 for a in answers.values())):
        raise ValueError('Supply a nonempty JSON object of current pending keys to answer strings (max 2000 characters).')
    return {'run_id': report['run_id'], 'answers': answers}


def current_report(directory):
    from linuxstudio_common import write_report
    from linuxstudio_session import process_running
    report = read_json(directory / 'report.json')
    if report.get('status') not in ('complete', 'error', 'cancelled'):
        try:
            alive = process_running(read_json(directory / 'worker.json')['pid'])
        except (OSError, KeyError, ValueError):
            alive = False
        if not alive:
            report.update(status='error', error='Worker stopped unexpectedly; inspect any unknown submission outcome before restarting.')
            write_report(directory / 'report.json', report)
    return report


def authenticated(account=None, allow_prompt=False):
    import requests
    from linuxstudio_session import attach_session
    client = requests.Session()
    try:
        auth = attach_session(client, account, allow_prompt=allow_prompt)
    except Exception:
        client.close()
        raise
    return client, auth


def execute(args):
    from runtime_paths import DATA_DIR, JOBS_DIR
    from linuxstudio_common import catalog, progress, write_report
    from linuxstudio_session import (SessionStore, window_status, window_running,
        start_window, atomic_write, LOGOUT_PATH)
    if args.command == 'doctor':
        import tkinter
        import requests
        import bs4
        return {'version': '1.0.0', 'platform': sys.platform, 'python': sys.version.split()[0],
                'supported': os.name == 'nt', 'data_dir': str(DATA_DIR),
                'dependencies': {'requests': requests.__version__, 'beautifulsoup4': bs4.__version__,
                                 'tk': tkinter.TkVersion}, 'browser_required': False}
    if os.name != 'nt':
        raise RuntimeError('Version 1 authentication requires Windows (DPAPI and Tk).')
    if args.command == 'login':
        client, auth = authenticated(args.account, allow_prompt=True)
        client.close()
        start_window(show=True)
        return {'status': 'authenticated', 'username': auth.username,
                'session_reused': auth.session_reused, 'password_saved': auth.password_saved,
                'practice_submitted': False}
    if args.command == 'session':
        record = SessionStore().load()
        if args.action == 'show':
            if not window_running():
                raise RuntimeError('No session window is running. Use login to open one.')
            start_window(show=True)
            return {'status': 'show_requested'}
        if args.action == 'logout':
            if record:
                SessionStore().invalidate(record['id'])
                atomic_write(LOGOUT_PATH, json.dumps({'session_id': record['id']}).encode())
            return {'status': 'logged_out_locally', 'server_logout_requested': bool(record and window_running())}
        status = window_status()
        active = bool(record and record.get('active') and window_running() and
                      status.get('session_id') == record['id'] and status.get('status') == 'authenticated')
        result = {'status': 'authenticated' if active else 'unavailable',
                  'username': record.get('username') if record else None, 'window_running': window_running()}
        if args.verify:
            client, auth = authenticated()
            client.close()
            result.update(status='authenticated', username=auth.username, verified=True)
        return result
    if args.command in ('catalog', 'progress'):
        client, auth = authenticated(args.account)
        with client:
            topics = catalog(client)
            if args.command == 'catalog':
                return {'username': auth.username, 'chapter': 'Linux常用命令', 'commands': topics}
            names = args.commands or list(topics)
            if not set(names).issubset(topics):
                raise ValueError('Unknown command. Read catalog first.')
            return {'username': auth.username, 'progress': progress(client, names)}
    if args.command == 'job':
        if args.action == 'start':
            if len(args.commands) != len(set(args.commands)):
                raise ValueError('Duplicate command names.')
            client, auth = authenticated(args.account)
            with client:
                topics = catalog(client)
            if not set(args.commands).issubset(topics):
                raise ValueError('Unknown command. Read catalog first.')
            # Serialize creation; a second worker must not race the same PHP exercise.
            with SessionStore(JOBS_DIR / 'creation').locked():
                for old in JOBS_DIR.glob('*/report.json'):
                    previous = current_report(old.parent)
                    if (previous.get('status') not in ('complete', 'error', 'cancelled') and
                        previous.get('username') == auth.username):
                        raise RuntimeError('An unfinished job exists for this account: ' + old.parent.name + '. Cancel or finish it first.')
                job_id = str(uuid.uuid4())
                directory = job_path(job_id)
                directory.mkdir(parents=True)
                write_report(directory / 'catalog.json', {n: topics[n] for n in args.commands})
                write_report(directory / 'report.json', {'status': 'starting', 'username': auth.username,
                                                       'commands': args.commands})
                command = [sys.executable, str(Path(__file__).with_name('practice_worker.py')),
                    '--reuse-only', '--login', auth.username, '--commands', *args.commands,
                    '--catalog-file', str(directory / 'catalog.json'),
                    '--report', str(directory / 'report.json'), '--control-file', str(directory / 'control.json')]
                with (directory / 'worker.log').open('wb') as log:
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                               creationflags=subprocess.CREATE_NO_WINDOW)
                write_report(directory / 'worker.json', {'pid': process.pid, 'created_at': time.time()})
            return {'job_id': job_id, 'status': 'starting', 'username': auth.username,
                    'report_path': str(directory / 'report.json')}
        directory = job_path(args.job_id)
        report = current_report(directory)
        if args.action == 'status':
            return report
        if args.action == 'pending':
            return {k: report.get(k) for k in ('status', 'username', 'run_id', 'pending', 'error')}
        if args.action == 'cancel':
            if report.get('status') in ('complete', 'error', 'cancelled'):
                return {'status': report['status'], 'job_id': args.job_id}
            write_report(directory / 'cancel.json', {'cancelled': True})
            return {'status': 'cancellation_requested', 'job_id': args.job_id}
        if args.action == 'submit':
            with SessionStore(directory / 'submission').locked():
                report = read_json(directory / 'report.json')
                payload = validated_answers(report, read_json(args.answers_file))
                control = directory / 'control.json'
                if control.exists():
                    old = read_json(control)
                    pending_keys = {p['key'] for p in report['pending']}
                    if old.get('run_id') == report['run_id'] and pending_keys.intersection(old.get('answers', {})):
                        raise RuntimeError('Previous answers have not been consumed. Wait for updated pending keys.')
                write_report(control, payload)
            return {'status': 'answers_queued', 'count': len(payload['answers'])}
        if args.action == 'verify':
            if report.get('status') != 'complete' or set(report.get('completed_commands', [])) != set(report['commands']):
                raise RuntimeError('All stages have not been confirmed complete. Read job status.')
            client, auth = authenticated(report['username'])
            with client:
                current = progress(client, report['commands'])
            verified = all(current.get(n, {}).get('completed') for n in report['commands'])
            if not verified:
                raise RuntimeError('Account progress has not lit for every requested command.')
            return {'status': 'verified', 'username': auth.username, 'account_progress_verified': True,
                    'progress': current, 'elapsed_total_seconds': report.get('elapsed_total_seconds'),
                    'total_stages_passed': report.get('total_stages_passed')}
    raise ValueError('Unsupported command.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', help='Private runtime root; defaults to LocalAppData/LinuxStudioPractice.')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor')
    login = commands.add_parser('login')
    login.add_argument('--account')
    session = commands.add_parser('session')
    session.add_argument('action', choices=['status', 'show', 'logout'])
    session.add_argument('--verify', action='store_true')
    for name in ('catalog', 'progress'):
        item = commands.add_parser(name)
        item.add_argument('--account')
        if name == 'progress':
            item.add_argument('--commands', nargs='+')
    job = commands.add_parser('job').add_subparsers(dest='action', required=True)
    start = job.add_parser('start')
    start.add_argument('--commands', nargs='+', required=True)
    start.add_argument('--account')
    for name in ('status', 'pending', 'submit', 'verify', 'cancel'):
        item = job.add_parser(name)
        item.add_argument('job_id')
        if name == 'submit':
            item.add_argument('--answers-file', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.data_dir:
        os.environ['LINUXSTUDIO_DATA_DIR'] = str(Path(args.data_dir).expanduser().resolve())
    try:
        result = execute(args)
    except Exception as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
