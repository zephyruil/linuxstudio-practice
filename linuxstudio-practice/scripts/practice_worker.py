"""Authenticated HTTP practice worker driven by explicit agent answers.

Accounts are selected locally with optional encrypted password saving.
Every server-rendered stage must receive an
explicit answer via the local control file; no stages or completion flags are
fabricated. Own login cookies are encrypted by the persistent owner window.
"""
import argparse
import json
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urldefrag, urlsplit

import requests

from linuxstudio_common import BASE, VERSION, soup_for, verify_identity, write_report, progress
from linuxstudio_login import login_interactive


class TimedSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.request_seconds = 0.0
        self.request_count = 0

    def request(self, *args, **kwargs):
        start = time.perf_counter()
        try:
            return super().request(*args, **kwargs)
        finally:
            self.request_seconds += time.perf_counter() - start
            self.request_count += 1


def parse_stage(context, username):
    page = context['page']
    verify_identity(page, username)
    form, node = page.select_one('form#form_prac'), page.select_one('#prac_task')
    if form is None or node is None:
        raise RuntimeError('Expected stage form not found for ' + context['name'])
    task = re.sub(r'\s+', ' ', node.get_text(' ', strip=True))
    task = re.split(r'恭喜您|很遗憾', task)[0].strip()
    total = re.search(r'共\s*(\d+)\s*关', task)
    stage = re.search(r'第\s*(\d+)\s*关', task)
    if total is None or stage is None or int(stage[1]) != context['stage']:
        raise RuntimeError('Unexpected stage for ' + context['name'])
    context['total'] = int(total[1])
    payload = {e['name']: e.get('value', '') for e in form.select('input[name]')
               if e.get('type', 'text').lower() in ('hidden', 'submit')}
    if payload.get('step') != str(context['stage']):
        raise RuntimeError('Hidden step differs from rendered stage.')
    destination = urldefrag(urljoin(context['url'], form.get('action') or context['url']))[0]
    if destination != context['url'] or form.get('method', '').lower() != 'post':
        raise RuntimeError('Unexpected submission destination or method.')
    if form.select_one('textarea[name="ta_input"]') is None:
        raise RuntimeError('Answer textarea not found.')
    context['payload'] = payload
    context['task'] = task
    return {'key': f"{context['name']}:{context['stage']}:{context['attempt']}",
            'command': context['name'], 'stage': context['stage'],
            'total': context['total'], 'task': task}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--login', help='Optional suggested username; the local picker can select another account.')
    parser.add_argument('--commands', nargs='+', required=True)
    parser.add_argument('--catalog-file', type=Path, required=True)
    parser.add_argument('--reuse-only', action='store_true', help='Require the active session; never open or show a login window.')
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--control-file', type=Path, required=True)
    parser.add_argument('--lesson-file', type=Path)
    parser.add_argument('--max-errors', type=int, default=3)
    parser.add_argument('--reviewed-failures-file',type=Path)
    args = parser.parse_args()
    if args.max_errors < 1:
        parser.error('--max-errors must be positive')
    available = json.loads(args.catalog_file.read_text(encoding='utf-8'))
    topics = {name: available[name] for name in args.commands}
    if any(not re.fullmatch(r'1_\d+_' + re.escape(name), topic) for name, topic in topics.items()):
        raise RuntimeError('Invalid topic mapping.')
    report = {'status': 'starting', 'username': args.login, 'worker_version': VERSION,
              'run_id': str(uuid.uuid4()), 'commands': list(topics),
              'transport': 'Python requests.Session; HTTP only',
              'browser_foreground_used': False, 'password_saved': False,
              'session_saved': False, 'submissions': [], 'completed_commands': [],
              'requested_command_count':len(topics), 'max_errors':args.max_errors,
              'error_count':0, 'error_budget_scope':'command',
              'command_error_counts':{n:0 for n in topics}, 'skipped_commands':[], 'command_stage_counts':{},
              'reviewed_previous_failed_jobs':json.loads(args.reviewed_failures_file.read_text(encoding='utf-8')) if args.reviewed_failures_file else [],
              'lesson_evidence':json.loads(args.lesson_file.read_text(encoding='utf-8')) if args.lesson_file else []}
    def check_halt():
        halt = args.report.with_name('halt.json')
        if halt.exists():
            issue = json.loads(halt.read_text(encoding='utf-8'))
            report['execution_issue'] = issue
            raise RuntimeError('Execution stopped: ' + issue['reason'])
        if args.report.with_name('cancel.json').exists():
            raise RuntimeError('Job cancelled locally; completed submissions remain on the website.')
    def apply_skips():
        path=args.report.with_name('skip_requests.json')
        if not path.exists():
            return False
        requests=json.loads(path.read_text(encoding='utf-8'))
        changed=False
        for c in contexts:
            if c['name'] in requests and not c['complete'] and not c['skipped']:
                c['skipped']=True
                last=next((s for s in reversed(report['submissions']) if s['command']==c['name']),{})
                report['skipped_commands'].append({'command':c['name'],'stage':c['stage'],'total':c.get('total'),
                    'task':c.get('task'),'last_answer':last.get('answer'),'feedback':last.get('feedback'),
                    'error_count':report['command_error_counts'][c['name']], 'reason':requests[c['name']],
                    'source':'explicit_skip_request'})
                changed=True
        return changed
    try:
        with TimedSession() as session:
            task_started = time.perf_counter()
            session.headers.update({'User-Agent': 'LinuxStudioPracticeHTTP/1.0', 'Cache-Control': 'no-cache'})
            if args.reuse_only:
                from linuxstudio_session import attach_session
                auth = attach_session(session, args.login, args.report, allow_prompt=False)
            else:
                auth = login_interactive(session, args.login, args.report)
            args.login = auth.username
            report.update(username=auth.username, password_saved=auth.password_saved,
                          used_saved_account=auth.used_saved_account,
                          session_reused=auth.session_reused,
                          persistent_session=auth.persistent_session,
                          session_saved=auth.persistent_session)
            started = time.perf_counter()
            baseline_http = session.request_seconds
            baseline_count = session.request_count
            contexts = []
            network_seconds = 0.0
            for name, topic in topics.items():
                url = BASE + '/study/practice/practice.php?topic=' + topic
                t = time.perf_counter()
                response = session.get(url, timeout=(10, 20))
                network_seconds += time.perf_counter() - t
                page = soup_for(response)
                verify_identity(page, args.login)
                contexts.append({'name': name, 'url': url, 'page': page,
                                 'stage': 1, 'attempt': 1, 'complete': False, 'skipped':False})
            report['authenticated'] = True
            report['account_progress_before'] = progress(session, topics)
            deadline = time.monotonic() + 1800
            while not all(c['complete'] or c['skipped'] for c in contexts):
                check_halt()
                if apply_skips():
                    continue
                if args.lesson_file:
                    report['lesson_evidence'] = json.loads(args.lesson_file.read_text(encoding='utf-8'))
                covered = {lesson['command'] for receipt in report['lesson_evidence'] for lesson in receipt['lessons']}
                pending = [parse_stage(c, args.login) | {'needs_lesson':
                    report['command_error_counts'][c['name']]>0 and c['name'] not in covered}
                    for c in contexts if not c['complete'] and not c['skipped']]
                report['command_stage_counts'] = {c['name']:c['total'] for c in contexts}
                report.update(status='awaiting_answers', pending=pending,
                              elapsed_after_login_seconds=round(time.perf_counter() - started, 3))
                write_report(args.report, report)
                plan_changed=False
                while True:
                    check_halt()
                    if apply_skips():
                        plan_changed=True
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError('Timed out waiting for local answers.')
                    try:
                        control = json.loads(args.control_file.read_text(encoding='utf-8-sig'))
                    except (FileNotFoundError, PermissionError, json.JSONDecodeError):
                        control = {}
                    answers = control.get('answers', {}) if control.get('run_id') == report['run_id'] else {}
                    if any(p['key'] in answers for p in pending):
                        break
                    time.sleep(0.1)
                if plan_changed:
                    continue
                report['status'] = 'submitting'
                if args.lesson_file:
                    report['lesson_evidence'] = json.loads(args.lesson_file.read_text(encoding='utf-8'))
                write_report(args.report, report)
                for context in contexts:
                    check_halt()
                    apply_skips()
                    if context['complete'] or context['skipped']:
                        continue
                    key = f"{context['name']}:{context['stage']}:{context['attempt']}"
                    if key not in answers:
                        continue
                    answer = answers[key]
                    if not isinstance(answer, str) or not answer.strip() or len(answer) > 2000:
                        raise RuntimeError('Missing or invalid local answer.')
                    payload = dict(context['payload'])
                    payload['ta_input'] = answer
                    report['in_flight'] = {'command': context['name'], 'stage': context['stage'],
                                           'attempt': context['attempt']}
                    write_report(args.report, report)
                    t = time.perf_counter()
                    # Never retry a POST automatically: an interrupted result is unknown.
                    response = session.post(context['url'], data=payload,
                        headers={'Referer': context['url'], 'Origin': BASE}, timeout=(10, 20))
                    elapsed = time.perf_counter() - t
                    network_seconds += elapsed
                    page = soup_for(response)
                    verify_identity(page, args.login)
                    node = page.select_one('#item')
                    if node is None:
                        raise RuntimeError('No feedback returned.')
                    feedback = re.sub(r'\s+', ' ', node.get_text(' ', strip=True))
                    final = context['stage'] == context['total']
                    passed = ('成功通关' in feedback) if final else bool(re.search(
                        r'通过了第\s*' + str(context['stage']) + r'\s*关', feedback))
                    if passed and not final:
                        next_step = page.select_one('form#form_prac input[name="step"]')
                        passed = next_step is not None and next_step.get('value') == str(context['stage'] + 1)
                    report['submissions'].append({'command': context['name'], 'stage': context['stage'],
                        'total':context['total'],
                        'attempt': context['attempt'], 'task': context['task'], 'answer': answer,
                        'passed': passed, 'feedback': feedback, 'http_seconds': round(elapsed, 3)})
                    report.pop('in_flight', None)
                    context['page'] = page
                    if passed:
                        if final:
                            context['complete'] = True
                            report['completed_commands'].append(context['name'])
                        else:
                            context['stage'] += 1
                            context['attempt'] = 1
                    else:
                        report['error_count'] += 1
                        report['command_error_counts'][context['name']] += 1
                        if report['command_error_counts'][context['name']] >= args.max_errors:
                            context['skipped'] = True
                            report['skipped_commands'].append({'command':context['name'],
                                'stage':context['stage'], 'total':context['total'],
                                'task':context['task'], 'last_answer':answer, 'feedback':feedback,
                                'error_count':report['command_error_counts'][context['name']],
                                'reason':'Per-command rejection budget reached; deferred while other commands continue.'})
                        context['attempt'] += 1
                    report['http_practice_seconds_total'] = round(network_seconds, 3)
                    write_report(args.report, report)

            report['account_progress_after'] = progress(session, topics)
            report['completed_subset_progress_verified'] = all(
                report['account_progress_after'].get(name, {}).get('completed') for name in report['completed_commands'])
            report['account_progress_verified'] = not report['skipped_commands'] and report['completed_subset_progress_verified']
            report['elapsed_after_login_seconds'] = round(time.perf_counter() - started, 3)
            report['elapsed_total_seconds'] = round(time.perf_counter() - task_started, 3)
            report['total_stages_passed'] = sum(s['passed'] for s in report['submissions'])
            report['http_request_seconds_total'] = round(session.request_seconds - baseline_http, 3)
            report['http_request_count'] = session.request_count - baseline_count
            report['pending'] = []
            if not report['completed_subset_progress_verified']:
                raise RuntimeError('All command stages passed, but account progress was not fully lit.')
            report['status'] = 'partial' if report['skipped_commands'] else 'complete'
            report['requires_user_review'] = False
            write_report(args.report, report)
    except Exception as exc:
        report.update(status='error', error=str(exc), requires_user_review=True)
        report['elapsed_total_seconds'] = round(time.perf_counter() - task_started, 3) if 'task_started' in locals() else 0
        report['total_stages_passed'] = sum(s['passed'] for s in report['submissions'])
        if 'contexts' in locals():
            report['pending'] = [{'command':c['name'], 'stage':c['stage'],
                'total':c.get('total'), 'task':c.get('task')} for c in contexts if not c['complete'] and not c['skipped']]
        if report.get('in_flight'):
            report['submission_outcome_unknown'] = True
        write_report(args.report, report)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
