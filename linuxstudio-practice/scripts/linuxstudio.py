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


def halt_job(directory, category, reason):
    from linuxstudio_common import write_report
    write_report(directory / 'halt.json', {'category':category, 'reason':reason, 'at':time.time()})


def summarize_report(directory, report):
    verification_path = directory / 'verification.json'
    verification = read_json(verification_path) if verification_path.exists() else {}
    current = verification.get('progress', {})
    items = []
    for name in report['commands']:
        passed = {s['stage'] for s in report.get('submissions',[]) if s['command']==name and s['passed']}
        total = report.get('command_stage_counts',{}).get(name)
        all_stages = bool(total and len(passed)==total and name in report.get('completed_commands',[]))
        verified = bool(all_stages and current.get(name,{}).get('completed'))
        items.append({'command':name, 'passed_stages':len(passed), 'total_stages':total,
            'all_stages_passed':all_stages, 'account_progress_verified':verified,
            'newly_lit':verified and report.get('account_progress_before',{}).get(name,{}).get('completed') is False,
            'next_stage':None if all_stages else len(passed)+1})
    failures = [s for s in report.get('submissions',[]) if not s['passed']]
    counts = {name:sum(s['command']==name for s in failures) for name in report['commands']}
    scope = report.get('error_budget_scope','job')
    return {'job_id':directory.name, 'username':report.get('username'), 'status':report['status'],
        'worker_version':report.get('worker_version'),
        'requested_commands':report['commands'], 'requested_count':len(report['commands']),
        'verified_count':sum(i['account_progress_verified'] for i in items), 'commands':items,
        'max_errors':report.get('max_errors',3), 'judge_error_count':len(failures),
        'error_budget_scope':scope, 'command_error_counts':counts,
        'remaining_error_budget':max(0,report.get('max_errors',3)-len(failures)) if scope=='job' else None,
        'remaining_budget_per_command':{n:max(0,report.get('max_errors',3)-c) for n,c in counts.items()} if scope=='command' else {},
        'skipped_commands':report.get('skipped_commands',[]),
        'skipped_count':len(report.get('skipped_commands',[])),
        'stopped_by_error_budget':report.get('stopped_by_error_budget',False),
        'stop_reason':report.get('error'), 'requires_user_review':report.get('requires_user_review',False),
        'execution_issue':report.get('execution_issue'),
        'all_failed_submissions':failures, 'lesson_evidence':report.get('lesson_evidence',[]),
        'lesson_evidence_limit':'Records show fetched content, not whether the model understood it.',
        'reviewed_previous_failed_jobs':report.get('reviewed_previous_failed_jobs',[]),
        'elapsed_total_seconds':report.get('elapsed_total_seconds'),
        'submission_outcome_unknown':report.get('submission_outcome_unknown',False)}


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
    if report.get('status') not in ('complete', 'partial', 'error', 'cancelled'):
        try:
            alive = process_running(read_json(directory / 'worker.json')['pid'])
        except (OSError, KeyError, ValueError):
            alive = False
        if not alive:
            report.update(status='error', requires_user_review=True,
                          error='Worker stopped unexpectedly; inspect any unknown submission outcome before restarting.')
            write_report(directory / 'report.json', report)
    return report


def submission_in_progress(directory, report):
    if report['status'] in ('starting', 'submitting'):
        return True
    if report['status'] != 'awaiting_answers':
        return False
    try:
        control = read_json(directory / 'control.json')
    except FileNotFoundError:
        return False
    keys = {p['key'] for p in report.get('pending', [])}
    return control.get('run_id') == report.get('run_id') and bool(keys.intersection(control.get('answers', {})))


def pending_snapshot(directory, report):
    busy = submission_in_progress(directory, report)
    result = {k: report.get(k) for k in ('status', 'username', 'run_id', 'error', 'error_count', 'max_errors',
                                        'command_error_counts', 'skipped_commands')}
    result['worker_version'] = report.get('worker_version')
    result['skipped_commands'] = [{k:s.get(k) for k in ('command','stage','error_count','reason')}
                                  for s in report.get('skipped_commands', [])]
    result['submission_in_progress'] = busy
    result['pending'] = [] if busy else [dict(p) for p in report.get('pending', [])]
    if busy and result['status'] == 'awaiting_answers':
        result['status'] = 'answers_queued'
    # Return only feedback needed for the current retry, not the full job history.
    for item in result['pending']:
        last = next((s for s in reversed(report.get('submissions', []))
                     if s['command'] == item['command'] and s['stage'] == item['stage']), None)
        if last and not last['passed']:
            item['last_rejection'] = {k: last[k] for k in ('attempt', 'answer', 'feedback')}
    return result


def compact_status(report):
    return {k: report.get(k) for k in ('status', 'username', 'worker_version', 'requested_command_count',
        'completed_commands', 'error_count', 'command_error_counts', 'error_budget_scope', 'max_errors',
        'elapsed_total_seconds', 'elapsed_after_login_seconds', 'error',
        'requires_user_review', 'submission_outcome_unknown')} | {
            'pending_count': len(report.get('pending', [])),
            'skipped_commands':[{k:s.get(k) for k in ('command','stage','error_count','reason')}
                                for s in report.get('skipped_commands', [])]}


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
    from linuxstudio_common import (BASE, VERSION, catalog, progress, write_report, soup_for,
                                   save_lesson_receipt, load_lesson_evidence)
    from linuxstudio_session import (SessionStore, window_status, window_running,
        start_window, atomic_write, LOGOUT_PATH)
    if args.command == 'doctor':
        import tkinter
        import requests
        import bs4
        return {'version': VERSION, 'platform': sys.platform, 'python': sys.version.split()[0],
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
    if args.command in ('catalog', 'progress', 'lesson'):
        client, auth = authenticated(args.account)
        with client:
            topics = catalog(client)
            if args.command == 'catalog':
                return {'username': auth.username, 'chapter': 'Linux常用命令', 'commands': topics}
            names = args.commands or list(topics)
            if not set(names).issubset(topics):
                raise ValueError('Unknown command. Read catalog first.')
            if args.command == 'lesson':
                lessons = []
                for name in names:
                    url = BASE + '/study/content/content_' + topics[name] + '.php'
                    page = soup_for(client.get(url, timeout=(10, 20)))
                    for element in page.select('script, style'):
                        element.decompose()
                    body = page.select_one('#item') or page.body or page
                    text = body.get_text('\n', strip=True)
                    if not text.strip():
                        raise RuntimeError('Empty lesson; inspect the tutorial link on the site.')
                    lessons.append({'command':name, 'topic':topics[name], 'url':url, 'text':text})
                receipt = save_lesson_receipt(auth.username, lessons)
                return {'username':auth.username, 'source':'LinuxStudio own tutorials', 'lessons':lessons,
                        'lesson_receipt_id':receipt['id'],
                        'practice_submitted':False}
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
            selected = {n:topics[n] for n in args.commands}
            lessons = load_lesson_evidence(args.lesson_receipts, auth.username, selected, require_all=False)
            # Serialize creation; a second worker must not race the same PHP exercise.
            with SessionStore(JOBS_DIR / 'creation').locked():
                reviewed = []
                for old in JOBS_DIR.glob('*/report.json'):
                    previous = current_report(old.parent)
                    if previous.get('username') != auth.username:
                        continue
                    if previous.get('requires_user_review'):
                        if (previous.get('stopped_by_error_budget') and not previous.get('execution_issue')
                                and not previous.get('submission_outcome_unknown')):
                            reviewed.append({'job_id':old.parent.name, 'status':previous['status'],
                                'error':previous.get('error'), 'policy_note':'Legacy judge-only stop is deferred under v1.2; original record retained.'})
                            continue
                        if not (old.parent / 'acknowledged.json').exists():
                            raise RuntimeError('A previous job requires human review: ' + old.parent.name +
                                '. Do not restart to reset the error budget. Ask the user first.')
                        reviewed.append({'job_id':old.parent.name, 'status':previous['status'],
                            'error':previous.get('error'), 'error_count':previous.get('error_count'),
                            'acknowledgment':read_json(old.parent / 'acknowledged.json')})
                    if (previous.get('status') not in ('complete', 'partial', 'error', 'cancelled') and
                        previous.get('username') == auth.username):
                        halt_job(old.parent, 'concurrency_conflict', 'Another job start was attempted for the same account.')
                        raise RuntimeError('Concurrent job start blocked; existing job halted for human review: ' + old.parent.name)
                job_id = str(uuid.uuid4())
                directory = job_path(job_id)
                directory.mkdir(parents=True)
                write_report(directory / 'catalog.json', {n: topics[n] for n in args.commands})
                write_report(directory / 'lessons.json', lessons)
                write_report(directory / 'reviewed_failures.json', reviewed)
                write_report(directory / 'report.json', {'status': 'starting', 'username': auth.username,
                                                       'commands': args.commands})
                command = [sys.executable, str(Path(__file__).with_name('practice_worker.py')),
                    '--reuse-only', '--login', auth.username, '--commands', *args.commands,
                    '--catalog-file', str(directory / 'catalog.json'),
                    '--lesson-file', str(directory / 'lessons.json'), '--max-errors',str(args.max_errors),
                    '--reviewed-failures-file', str(directory / 'reviewed_failures.json'),
                    '--report', str(directory / 'report.json'), '--control-file', str(directory / 'control.json')]
                with (directory / 'worker.log').open('wb') as log:
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                               creationflags=subprocess.CREATE_NO_WINDOW)
                write_report(directory / 'worker.json', {'pid': process.pid, 'created_at': time.time()})
            return {'job_id': job_id, 'status': 'starting', 'username': auth.username,
                    'requested_command_count':len(args.commands), 'commands':args.commands,
                    'max_errors':args.max_errors, 'lesson_evidence_count':len(lessons),
                    'error_budget_scope':'command',
                    'report_path': str(directory / 'report.json')}
        directory = job_path(args.job_id)
        report = current_report(directory)
        if args.action == 'status':
            return compact_status(report) if getattr(args, 'compact', False) else report
        if args.action == 'pending':
            deadline = time.monotonic() + getattr(args, 'wait', 0)
            while True:
                snapshot = pending_snapshot(directory, report)
                if not snapshot['submission_in_progress'] or time.monotonic() >= deadline:
                    return snapshot
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                report = current_report(directory)
        if args.action == 'attach-lessons':
            if report['status']!='awaiting_answers':
                raise RuntimeError('Attach lessons while the job is awaiting answers, not after completion.')
            with SessionStore(directory / 'lesson_update').locked():
                topics = read_json(directory / 'catalog.json')
                records = load_lesson_evidence(args.lesson_receipts, report['username'],topics,require_all=False)
                existing = read_json(directory / 'lessons.json')
                combined = {r['id']:r for r in [*existing,*records]}
                write_report(directory / 'lessons.json',list(combined.values()))
            return {'status':'lessons_attached','receipt_ids':list(combined),'practice_submitted':False}
        if args.action == 'skip':
            if report['status']!='awaiting_answers':
                raise RuntimeError('Wait until the job is awaiting answers before deferring commands.')
            pending={p['command'] for p in report['pending']}
            if not args.reason.strip() or not set(args.commands).issubset(pending):
                raise ValueError('Provide pending command names and a nonempty deferral reason.')
            with SessionStore(directory/'skip_update').locked():
                path=directory/'skip_requests.json'
                requests=read_json(path) if path.exists() else {}
                requests.update({name:args.reason for name in args.commands})
                write_report(path,requests)
            return {'status':'skip_requested','commands':args.commands,'reason':args.reason,'practice_submitted':False}
        if args.action == 'acknowledge':
            if not report.get('requires_user_review'):
                raise RuntimeError('No stopped job requires acknowledgment.')
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            try:
                approved = messagebox.askyesno('LinuxStudio · 停止后的人工审核',
                    '作业：' + args.job_id + '\n停止原因：' + str(report.get('error')) +
                    '\n说明：' + args.reason + '\n\n确认已检查失败记录，并允许后续创建新作业？\n原失败记录会保留，不会自动恢复或提交。',parent=root)
            finally:
                root.destroy()
            if not approved:
                raise RuntimeError('User did not approve restarting; stop remains in effect.')
            write_report(directory / 'acknowledged.json', {'reason':args.reason,'at':time.time(),'confirmed_locally':True})
            return {'status':'review_acknowledged','job_id':args.job_id,'practice_submitted':False}
        if args.action == 'report':
            if args.verify_progress:
                client, auth = authenticated(report['username'])
                with client:
                    current = progress(client, report['commands'])
                write_report(directory / 'verification.json', {'username':auth.username,'progress':current,'at':time.time()})
            return summarize_report(directory, report)
        if args.action == 'cancel':
            if report.get('status') in ('complete', 'partial', 'error', 'cancelled'):
                return {'status': report['status'], 'job_id': args.job_id}
            write_report(directory / 'cancel.json', {'cancelled': True})
            return {'status': 'cancellation_requested', 'job_id': args.job_id}
        if args.action == 'submit':
            with SessionStore(directory / 'submission').locked():
                report = read_json(directory / 'report.json')
                if submission_in_progress(directory, report):
                    return {'status':'waiting_for_worker', 'job_id':args.job_id,
                        'answers_queued':False, 'practice_submitted':False,
                        'next_action':'Do not submit again. Use job pending --wait 3 for the next answer round.'}
                if report['status'] in ('complete', 'partial', 'error', 'cancelled'):
                    return {'status':'job_finished', 'job_status':report['status'], 'job_id':args.job_id,
                        'requires_user_review':report.get('requires_user_review',False),
                        'answers_queued':False, 'practice_submitted':False}
                try:
                    payload = validated_answers(report, read_json(args.answers_file))
                except (ValueError, OSError) as exc:
                    halt_job(directory, 'invalid_answer_file_or_state', str(exc))
                    raise
                evidence = read_json(directory / 'lessons.json') if (directory / 'lessons.json').exists() else []
                covered = {e['command'] for r in evidence for e in r['lessons']}
                needs = {p['command'] for p in report['pending'] if p['key'] in payload['answers']
                    and report.get('command_error_counts',{}).get(p['command'],0)>0 and p['command'] not in covered}
                if needs:
                    return {'status':'needs_lesson','commands':sorted(needs),'practice_submitted':False,
                        'next_action':'Fetch only these lessons, attach their receipt IDs, then resubmit. The job is still waiting.'}
                control = directory / 'control.json'
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
            write_report(directory / 'verification.json', {'username':auth.username,'progress':current,'at':time.time()})
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
    for name in ('catalog', 'progress', 'lesson'):
        item = commands.add_parser(name)
        item.add_argument('--account')
        if name in ('progress', 'lesson'):
            item.add_argument('--commands', nargs='+', required=name == 'lesson')
    job = commands.add_parser('job').add_subparsers(dest='action', required=True)
    start = job.add_parser('start')
    start.add_argument('--commands', nargs='+', required=True)
    start.add_argument('--account')
    start.add_argument('--lesson-receipts',nargs='+',default=[])
    start.add_argument('--max-errors',type=int,default=3,help='Total judge rejections per command before deferring it.')
    for name in ('status', 'pending', 'submit', 'verify', 'cancel', 'report', 'acknowledge','attach-lessons','skip'):
        item = job.add_parser(name)
        item.add_argument('job_id')
        if name == 'submit':
            item.add_argument('--answers-file', type=Path, required=True)
        elif name == 'pending':
            item.add_argument('--wait',type=float,default=0,help='Wait up to 30 seconds locally for a fresh answer round.')
        elif name == 'status':
            item.add_argument('--compact',action='store_true',help='Omit prompts, submissions and lesson hashes.')
        elif name == 'report':
            item.add_argument('--verify-progress',action='store_true')
        elif name == 'acknowledge':
            item.add_argument('--reason',required=True)
        elif name == 'attach-lessons':
            item.add_argument('--lesson-receipts',nargs='+',required=True)
        elif name == 'skip':
            item.add_argument('--commands',nargs='+',required=True)
            item.add_argument('--reason',required=True)
    args = parser.parse_args(argv)
    if args.command == 'job' and args.action == 'start' and args.max_errors < 1:
        parser.error('--max-errors must be positive')
    if args.command == 'job' and args.action == 'pending' and not 0 <= args.wait <= 30:
        parser.error('--wait must be between 0 and 30 seconds')
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
