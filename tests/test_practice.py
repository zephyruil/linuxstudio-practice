"""Offline stage/progress contract tests; no website requests or credentials."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'linuxstudio-practice' / 'scripts'))
from bs4 import BeautifulSoup
import requests
from linuxstudio_common import parse_catalog, progress
from linuxstudio import job_path, validated_answers, execute, summarize_report
from linuxstudio_login import LoginResult
import practice_worker as worker


def html(stage=1, total=2, feedback='', username='synthetic', hidden=None):
    return (f'{username} 已登录 <a href="/user/login.php?action=logout">退出</a>'
            f'<div id="prac_task">（共 {total} 关）第 {stage} 关 任务：创建目录</div>'
            f'<div id="item">{feedback}</div><form id="form_prac" method="post" action="#">'
            f'<input type="hidden" name="step" value="{stage if hidden is None else hidden}">'
            '<input type="hidden" name="success" value="server-token">'
            '<input type="hidden" name="submit_flag" value="opaque">'
            '<input type="submit" name="button_prac_process" value="执行">'
            '<textarea name="ta_input"></textarea></form>')


class Response:
    url = 'http://www.linuxstudio.cn/study/practice/practice.php?topic=1_60_mkdir'
    def __init__(self, text='', data=None):
        self.text, self.data = text, data
    def raise_for_status(self):
        pass
    def json(self):
        return self.data


class Site:
    request_seconds = 0
    request_count = 0
    def __init__(self, lit=True, wrong_account=False, reject=False, timeout=False, progress_names=None):
        self.headers = {}
        self.posts = []
        self.lit, self.wrong_account, self.reject, self.timeout = lit, wrong_account, reject, timeout
        self.progress_names=progress_names or ['mkdir']
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def get(self, url, **kwargs):
        if 'json_data' in url:
            return Response(data={'data': [{'name': n, 'value': 1} for n in self.progress_names]})
        if 'json_color' in url:
            return Response(data=['#FE8463' if self.lit and len(self.posts) >= 2 else '#fcc3b4']*len(self.progress_names))
        return Response(html(username='other' if self.wrong_account else 'synthetic'))
    def post(self, url, data, **kwargs):
        self.posts.append(dict(data))
        if self.timeout:
            raise requests.Timeout('Synthetic timeout; result unknown.')
        if self.reject:
            return Response(html(feedback='很遗憾，未通过'))
        if data['step'] == '1':
            return Response(html(stage=2, feedback='恭喜您，通过了第 1 关'))
        return Response(html(stage=2, feedback='mkdir 成功通关'))


class PracticeTests(unittest.TestCase):
    def run_worker(self, site, transient_control_lock=False, commands=None, max_errors=3, halt_after_first=False, skip_initial=None):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            catalog = root / 'catalog.json'
            commands = commands or ['mkdir']
            catalog.write_text(json.dumps({n:'1_60_'+n for n in commands}), encoding='utf-8')
            report, control = root / 'report.json', root / 'control.json'
            original_read = Path.read_text
            denied = False
            def read_after_unlock(path, *args, **kwargs):
                nonlocal denied
                if transient_control_lock and path == control and not denied:
                    denied = True
                    raise PermissionError('synthetic transient reader conflict')
                return original_read(path, *args, **kwargs)
            from linuxstudio_common import write_report
            def answer_pending(path, value):
                write_report(path, value)
                if halt_after_first and len(value.get('submissions',[]))==1:
                    write_report(root/'halt.json',{'category':'concurrency_conflict','reason':'synthetic competing start'})
                if value['status'] == 'awaiting_answers':
                    if skip_initial:
                        write_report(root/'skip_requests.json',{skip_initial:'synthetic tutorial unavailable'})
                    control.write_text(json.dumps({'run_id': value['run_id'],
                        'answers': {p['key']: 'mkdir synthetic_dir' for p in value['pending']}}), encoding='utf-8')
            argv = ['worker', '--reuse-only', '--login', 'synthetic', '--commands', *commands,
                    '--max-errors',str(max_errors),
                    '--catalog-file', str(catalog), '--report', str(report), '--control-file', str(control)]
            with patch.object(sys, 'argv', argv), patch.object(worker, 'TimedSession', return_value=site), \
                 patch('linuxstudio_session.attach_session', return_value=LoginResult('synthetic', False, False)), \
                 patch.object(Path, 'read_text', read_after_unlock), \
                 patch.object(worker, 'write_report', side_effect=answer_pending):
                code = worker.main()
            if transient_control_lock:
                self.assertTrue(denied)
            return code, json.loads(report.read_text(encoding='utf-8'))

    def test_all_stages_real_fields_and_account_progress(self):
        site = Site()
        code, report = self.run_worker(site)
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['total_stages_passed'], 2)
        self.assertTrue(report['account_progress_verified'])
        from linuxstudio_common import VERSION
        self.assertEqual(report['worker_version'], VERSION)
        self.assertEqual([p['step'] for p in site.posts], ['1', '2'])
        self.assertTrue(all(p['success'] == 'server-token' and p['submit_flag'] == 'opaque' for p in site.posts))

    def test_all_stages_without_lit_progress_is_error(self):
        code, report = self.run_worker(Site(lit=False))
        self.assertEqual(code, 1)
        self.assertEqual(report['completed_commands'], ['mkdir'])
        self.assertFalse(report['account_progress_verified'])
        self.assertEqual(report['status'], 'error')

    def test_wrong_account_cannot_submit(self):
        site = Site(wrong_account=True)
        code, report = self.run_worker(site)
        self.assertEqual(code, 1)
        self.assertEqual(site.posts, [])

    def test_timeout_is_never_posted_twice(self):
        site = Site(timeout=True)
        code, report = self.run_worker(site)
        self.assertEqual(code, 1)
        self.assertEqual(len(site.posts), 1)
        self.assertEqual(report['completed_commands'], [])
        self.assertTrue(report['submission_outcome_unknown'])

    def test_transient_control_read_denial_does_not_abort_or_repeat_post(self):
        site = Site()
        code, report = self.run_worker(site, transient_control_lock=True)
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual([p['step'] for p in site.posts], ['1', '2'])

    def test_rejection_is_bounded_and_not_completion(self):
        site = Site(reject=True)
        code, report = self.run_worker(site)
        self.assertEqual(code, 0)
        self.assertEqual(len(site.posts), 3)
        self.assertEqual(report['error_count'],3)
        self.assertEqual(report['status'],'partial')
        self.assertEqual(report['skipped_commands'][0]['command'],'mkdir')
        self.assertFalse(report['requires_user_review'])
        self.assertFalse(report['account_progress_verified'])
        self.assertFalse(any(p['passed'] for p in report['submissions']))

    def test_budgets_are_per_command_and_all_skipped_commands_are_reported(self):
        site = Site(reject=True)
        code, report = self.run_worker(site, commands=['mkdir','rmdir'])
        self.assertEqual(code,0)
        self.assertEqual(report['status'],'partial')
        self.assertEqual(len(site.posts),6)
        self.assertEqual(report['command_error_counts'],{'mkdir':3,'rmdir':3})
        self.assertEqual(len(report['skipped_commands']),2)
        self.assertEqual(report['requested_command_count'],2)

    def test_success_between_errors_does_not_reset_budget(self):
        site = Site()
        original = site.post
        def alternating(url, data, **kwargs):
            if len(site.posts) in (0,2,3):
                site.posts.append(dict(data))
                return Response(html(stage=int(data['step']),feedback='很遗憾'))
            return original(url,data,**kwargs)
        site.post = alternating
        code, report = self.run_worker(site)
        self.assertEqual(code,0)
        self.assertEqual(report['status'],'partial')
        self.assertEqual([s['passed'] for s in report['submissions']], [False,True,False,False])
        self.assertEqual(report['error_count'],3)

    def test_user_selected_lower_budget_is_enforced(self):
        site = Site(reject=True)
        code, report = self.run_worker(site,max_errors=1)
        self.assertEqual(code,0)
        self.assertEqual(report['status'],'partial')
        self.assertEqual(len(site.posts),1)

    def test_healthy_command_continues_after_other_command_is_skipped(self):
        site=Site(progress_names=['mkdir','rmdir'])
        original_get=site.get
        def get(url,**kwargs):
            return Response(html(total=4)) if 'topic=1_60_rmdir' in url else original_get(url,**kwargs)
        def post(url,data,**kwargs):
            site.posts.append(dict(data))
            if url.endswith('mkdir'):
                return Response(html(feedback='很遗憾'))
            stage=int(data['step'])
            return Response(html(stage=stage+1,total=4,feedback=f'通过了第 {stage} 关')) if stage<4 else Response(html(stage=4,total=4,feedback='成功通关'))
        site.get=get;site.post=post
        code,report=self.run_worker(site,commands=['mkdir','rmdir'])
        self.assertEqual(code,0)
        self.assertEqual(report['status'],'partial')
        self.assertEqual(report['completed_commands'],['rmdir'])
        self.assertEqual([p['step'] for p in site.posts][-2:],['3','4'])
        self.assertTrue(report['completed_subset_progress_verified'])
        self.assertFalse(report['account_progress_verified'])

    def test_manual_skip_does_not_submit_that_command_or_block_others(self):
        site=Site(progress_names=['mkdir','rmdir'])
        code,report=self.run_worker(site,commands=['mkdir','rmdir'],skip_initial='mkdir')
        self.assertEqual(code,0)
        self.assertEqual(report['status'],'partial')
        self.assertEqual(report['completed_commands'],['rmdir'])
        self.assertEqual(len(site.posts),2)
        self.assertEqual(report['error_count'],0)
        self.assertEqual(report['skipped_commands'][0]['source'],'explicit_skip_request')

    def test_halt_is_checked_between_submissions_in_same_batch(self):
        site=Site()
        code,report=self.run_worker(site,commands=['mkdir','rmdir'],halt_after_first=True)
        self.assertEqual(code,1)
        self.assertEqual(len(site.posts),1)
        self.assertEqual(report['execution_issue']['category'],'concurrency_conflict')

    def test_hidden_stage_must_match_live_task(self):
        context = {'name': 'mkdir', 'stage': 1, 'attempt': 1,
                   'url': Response.url, 'page': BeautifulSoup(html(hidden=2), 'html.parser')}
        with self.assertRaises(RuntimeError):
            worker.parse_stage(context, 'synthetic')

    def test_form_destination_cannot_leave_exercise(self):
        context = {'name': 'mkdir', 'stage': 1, 'attempt': 1, 'url': Response.url,
                   'page': BeautifulSoup(html().replace('action="#"', 'action="http://example.org/"'), 'html.parser')}
        with self.assertRaises(RuntimeError):
            worker.parse_stage(context, 'synthetic')

    def test_catalog_restricts_site_chapter_and_topic_shape(self):
        page = BeautifulSoup('<a href="study/practice/practice.php?topic=1_60_mkdir">题</a>'
            '<a href="https://example.org/study/practice/practice.php?topic=1_50_ls">bad</a>'
            '<a href="/study/practice/practice.php?topic=2_10_vi">other chapter</a>'
            '<a href="/study/practice/practice.php?topic=../bad">bad</a>', 'html.parser')
        self.assertEqual(parse_catalog(page), {'mkdir': '1_60_mkdir'})

    def test_progress_length_mismatch_fails_closed(self):
        site = Site()
        with patch.object(site, 'get', side_effect=[Response(data={'data':[{'name':'mkdir'}]}), Response(data=[])]):
            with self.assertRaises(RuntimeError):
                progress(site, ['mkdir'])

    def test_progress_uses_actual_combined_site_labels(self):
        from linuxstudio_common import PROGRESS_NAMES
        names = ['background_command', 'zip', 'gzip', 'bzip2', 'pbzip2', 'ftp', 'who_w', 'pipeline']
        site = Site()
        data = {'data': [{'name': PROGRESS_NAMES[n]} for n in names]}
        with patch.object(site, 'get', side_effect=[Response(data=data), Response(data=['#FE8463'] * len(names))]):
            result = progress(site, names)
        self.assertEqual(set(result), set(names))
        self.assertTrue(all(item['completed'] for item in result.values()))
        self.assertEqual(result['zip']['site_progress_name'], 'zip、unzip')
        self.assertEqual(result['who_w']['site_progress_name'], 'who、w')
        self.assertEqual(result['pipeline']['site_progress_name'], '管道')

    def test_stale_or_invalid_answers_rejected(self):
        report = {'status': 'awaiting_answers', 'run_id': 'synthetic-run', 'pending': [{'key':'mkdir:2:1'}]}
        self.assertEqual(validated_answers(report, {'mkdir:2:1':'mkdir a'})['run_id'], 'synthetic-run')
        for answers in ({'mkdir:1:1':'mkdir a'}, {'mkdir:2:1':''}, {'mkdir:2:1':1}, {}):
            with self.assertRaises(ValueError):
                validated_answers(report, answers)
        with self.assertRaises(ValueError):
            validated_answers({**report, 'status':'submitting'}, {'mkdir:2:1':'mkdir a'})

    def test_job_path_rejects_traversal(self):
        for value in ('../../private', '..', 'C:\\Temp', 'not-an-id'):
            with self.assertRaises(ValueError):
                job_path(value)

    def test_lesson_fetches_catalog_topic_without_posting(self):
        site = Site()
        args = argparse.Namespace(command='lesson', commands=['head'], account=None)
        with patch('linuxstudio.authenticated', return_value=(site, LoginResult('synthetic',False,False))), \
             patch('linuxstudio_common.catalog', return_value={'head':'1_130_head'}), \
             patch('linuxstudio_common.save_lesson_receipt', return_value={'id':'synthetic-receipt'}), \
             patch.object(site, 'get', return_value=Response('<div id="item">head 默认显示前10行<pre>head file</pre></div>')) as get:
            result = execute(args)
        self.assertFalse(result['practice_submitted'])
        self.assertEqual(site.posts, [])
        self.assertIn('前10行', result['lessons'][0]['text'])
        self.assertEqual(result['lessons'][0]['url'], 'http://www.linuxstudio.cn/study/content/content_1_130_head.php')
        get.assert_called_once_with(result['lessons'][0]['url'], timeout=(10,20))

    def test_lesson_rejects_names_outside_catalog_before_fetch(self):
        site = Site()
        args = argparse.Namespace(command='lesson', commands=['../../private'], account=None)
        with patch('linuxstudio.authenticated', return_value=(site, LoginResult('synthetic',False,False))), \
             patch('linuxstudio_common.catalog', return_value={'head':'1_130_head'}), \
             patch.object(site,'get') as get:
            with self.assertRaises(ValueError):
                execute(args)
            get.assert_not_called()

    def test_failed_job_cannot_be_restarted_without_local_review(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            old=root/'synthetic-old'
            old.mkdir()
            (old/'report.json').write_text('{}')
            previous={'username':'synthetic','status':'error','requires_user_review':True}
            args=argparse.Namespace(command='job',action='start',commands=['mkdir'],account=None,
                lesson_receipts=['synthetic'],max_errors=3)
            with patch('runtime_paths.JOBS_DIR',root), patch('linuxstudio.current_report',return_value=previous), \
                 patch('linuxstudio.authenticated',return_value=(Site(),LoginResult('synthetic',False,False))), \
                 patch('linuxstudio_common.catalog',return_value={'mkdir':'1_60_mkdir'}), \
                 patch('linuxstudio_common.load_lesson_evidence',return_value=[]), \
                 patch('subprocess.Popen') as spawn:
                with self.assertRaisesRegex(RuntimeError,'human review'):
                    execute(args)
                spawn.assert_not_called()

    def test_start_copies_real_receipts_and_passes_budget_to_worker(self):
        from linuxstudio_common import save_lesson_receipt
        with tempfile.TemporaryDirectory() as temp, patch('runtime_paths.DATA_DIR',Path(temp)), \
             patch('runtime_paths.JOBS_DIR',Path(temp)/'jobs'):
            receipt=save_lesson_receipt('synthetic',[{'command':'mkdir','topic':'1_60_mkdir',
                'url':'http://www.linuxstudio.cn/study/content/content_1_60_mkdir.php','text':'synthetic lesson'}])
            args=argparse.Namespace(command='job',action='start',commands=['mkdir'],account=None,
                lesson_receipts=[receipt['id']],max_errors=3)
            with patch('linuxstudio.authenticated',return_value=(Site(),LoginResult('synthetic',False,False))), \
                 patch('linuxstudio_common.catalog',return_value={'mkdir':'1_60_mkdir'}),patch('subprocess.Popen') as spawn:
                spawn.return_value.pid=12345
                result=execute(args)
            report_path=Path(result['report_path'])
            self.assertEqual(result['requested_command_count'],1)
            self.assertEqual(result['max_errors'],3)
            self.assertEqual(json.loads(report_path.with_name('lessons.json').read_text())[0]['id'],receipt['id'])
            command=spawn.call_args.args[0]
            self.assertEqual(command[command.index('--max-errors')+1],'3')

    def test_concurrent_start_halts_existing_job_instead_of_waiting(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); old=root/'synthetic-old';old.mkdir()
            (old/'report.json').write_text('{}')
            previous={'username':'synthetic','status':'awaiting_answers'}
            args=argparse.Namespace(command='job',action='start',commands=['mkdir'],account=None,
                lesson_receipts=['synthetic'],max_errors=3)
            with patch('runtime_paths.JOBS_DIR',root), patch('linuxstudio.current_report',return_value=previous), \
                 patch('linuxstudio.authenticated',return_value=(Site(),LoginResult('synthetic',False,False))), \
                 patch('linuxstudio_common.catalog',return_value={'mkdir':'1_60_mkdir'}), \
                 patch('linuxstudio_common.load_lesson_evidence',return_value=[]), \
                 patch('subprocess.Popen') as spawn:
                with self.assertRaisesRegex(RuntimeError,'Concurrent'):
                    execute(args)
                spawn.assert_not_called()
            self.assertEqual(json.loads((old/'halt.json').read_text())['category'],'concurrency_conflict')

    def test_invalid_answer_json_halts_without_writing_control(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            report={'status':'awaiting_answers','commands':['mkdir'],'pending':[{'key':'mkdir:1:1'}]}
            (root/'report.json').write_text(json.dumps(report))
            answers=root/'answers.json';answers.write_text('{broken')
            args=argparse.Namespace(command='job',action='submit',job_id='synthetic',answers_file=answers)
            with patch('linuxstudio.job_path',return_value=root),patch('linuxstudio.current_report',return_value=report):
                with self.assertRaises(ValueError):execute(args)
            self.assertTrue((root/'halt.json').exists())
            self.assertFalse((root/'control.json').exists())

    def test_report_retains_failures_and_does_not_count_pre_lit_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'verification.json').write_text(json.dumps({'progress':{'mkdir':{'completed':True},'rmdir':{'completed':True}}}))
            report={'status':'error','commands':['mkdir','rmdir'],'command_stage_counts':{'mkdir':2,'rmdir':2},
                'completed_commands':['mkdir'],'max_errors':3,'requires_user_review':True,
                'submissions':[{'command':'mkdir','stage':1,'passed':True},{'command':'mkdir','stage':2,'passed':True},
                    {'command':'rmdir','stage':1,'passed':False}]}
            summary=summarize_report(root,report)
            self.assertEqual(summary['verified_count'],1)
            self.assertEqual(len(summary['all_failed_submissions']),1)
            self.assertFalse(summary['commands'][1]['account_progress_verified'])

    def test_review_requires_actual_local_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            report={'requires_user_review':True,'error':'synthetic failure'}
            args=argparse.Namespace(command='job',action='acknowledge',job_id='synthetic',reason='user requested review')
            with patch('linuxstudio.job_path',return_value=root),patch('linuxstudio.current_report',return_value=report), \
                 patch('tkinter.Tk'),patch('tkinter.messagebox.askyesno',return_value=False):
                with self.assertRaises(RuntimeError):execute(args)
            self.assertFalse((root/'acknowledged.json').exists())
            with patch('linuxstudio.job_path',return_value=root),patch('linuxstudio.current_report',return_value=report), \
                 patch('tkinter.Tk'),patch('tkinter.messagebox.askyesno',return_value=True) as confirm:
                result=execute(args)
                confirm.assert_called_once()
            self.assertEqual(result['status'],'review_acknowledged')
            self.assertTrue(json.loads((root/'acknowledged.json').read_text())['confirmed_locally'])

    def test_lesson_receipts_cover_exact_account_and_topic(self):
        from linuxstudio_common import save_lesson_receipt,load_lesson_evidence
        with tempfile.TemporaryDirectory() as temp, patch('runtime_paths.DATA_DIR',Path(temp)):
            lessons=[{'command':'head','topic':'1_130_head','url':'http://www.linuxstudio.cn/study/content/content_1_130_head.php',
                      'text':'synthetic head tutorial'}]
            receipt=save_lesson_receipt('synthetic',lessons)
            self.assertEqual(len(load_lesson_evidence([receipt['id']],'synthetic',{'head':'1_130_head'})),1)
            for user, topics in [('other',{'head':'1_130_head'}),('synthetic',{'tail':'1_140_tail'})]:
                with self.assertRaises(ValueError):load_lesson_evidence([receipt['id']],user,topics)

    def test_start_without_lessons_does_not_prefetch_or_block(self):
        with tempfile.TemporaryDirectory() as temp,patch('runtime_paths.DATA_DIR',Path(temp)), \
             patch('runtime_paths.JOBS_DIR',Path(temp)/'jobs'):
            args=argparse.Namespace(command='job',action='start',commands=['mkdir'],account=None,lesson_receipts=[],max_errors=3)
            site=Site()
            with patch('linuxstudio.authenticated',return_value=(site,LoginResult('synthetic',False,False))), \
                 patch('linuxstudio_common.catalog',return_value={'mkdir':'1_60_mkdir'}),patch('subprocess.Popen') as spawn, \
                 patch.object(site,'get') as get:
                spawn.return_value.pid=12345
                result=execute(args)
                get.assert_not_called()
            self.assertEqual(result['lesson_evidence_count'],0)

    def test_legacy_judge_budget_stop_no_longer_blocks_new_batch(self):
        with tempfile.TemporaryDirectory() as temp,patch('runtime_paths.DATA_DIR',Path(temp)), \
             patch('runtime_paths.JOBS_DIR',Path(temp)/'jobs'):
            old=Path(temp)/'jobs/old';old.mkdir(parents=True)
            (old/'report.json').write_text(json.dumps({'status':'error','username':'synthetic',
                'requires_user_review':True,'stopped_by_error_budget':True,'error':'old global budget reached'}))
            args=argparse.Namespace(command='job',action='start',commands=['mkdir'],account=None,lesson_receipts=[],max_errors=3)
            with patch('linuxstudio.authenticated',return_value=(Site(),LoginResult('synthetic',False,False))), \
                 patch('linuxstudio_common.catalog',return_value={'mkdir':'1_60_mkdir'}),patch('subprocess.Popen') as spawn:
                spawn.return_value.pid=12345
                result=execute(args)
            history=json.loads(Path(result['report_path']).with_name('reviewed_failures.json').read_text())
            self.assertIn('Legacy judge-only',history[0]['policy_note'])

    def test_partial_terminal_job_is_not_marked_as_crashed(self):
        from linuxstudio import current_report
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'report.json').write_text('{"status":"partial","commands":["mkdir"]}')
            self.assertEqual(current_report(root)['status'],'partial')

    def test_lessons_cannot_be_attached_after_batch_finished(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);report={'status':'partial','commands':['mkdir']}
            args=argparse.Namespace(command='job',action='attach-lessons',job_id='synthetic',lesson_receipts=['synthetic'])
            with patch('linuxstudio.job_path',return_value=root),patch('linuxstudio.current_report',return_value=report):
                with self.assertRaisesRegex(RuntimeError,'not after completion'):execute(args)

    def test_rejected_command_fetches_and_attaches_only_its_own_lesson(self):
        from linuxstudio_common import save_lesson_receipt
        with tempfile.TemporaryDirectory() as temp,patch('runtime_paths.DATA_DIR',Path(temp)):
            root=Path(temp)/'job';root.mkdir()
            report={'status':'awaiting_answers','commands':['mkdir','head'], 'username':'synthetic','run_id':'synthetic-run',
                'command_error_counts':{'mkdir':1,'head':0},
                'pending':[{'key':'mkdir:1:2','command':'mkdir'},{'key':'head:1:1','command':'head'}]}
            (root/'report.json').write_text(json.dumps(report));(root/'lessons.json').write_text('[]')
            (root/'catalog.json').write_text(json.dumps({'mkdir':'1_60_mkdir','head':'1_130_head'}))
            answers=root/'answers.json';answers.write_text(json.dumps({'mkdir:1:2':'mkdir synthetic','head:1:1':'head file'}))
            submit=argparse.Namespace(command='job',action='submit',job_id='synthetic',answers_file=answers)
            with patch('linuxstudio.job_path',return_value=root),patch('linuxstudio.current_report',return_value=report):
                result=execute(submit)
                self.assertEqual(result['status'],'needs_lesson')
                self.assertEqual(result['commands'],['mkdir'])
                self.assertFalse((root/'halt.json').exists())
                self.assertFalse((root/'control.json').exists())
                receipt=save_lesson_receipt('synthetic',[{'command':'mkdir','topic':'1_60_mkdir',
                    'url':'http://www.linuxstudio.cn/study/content/content_1_60_mkdir.php','text':'synthetic tutorial'}])
                attach=argparse.Namespace(command='job',action='attach-lessons',job_id='synthetic',lesson_receipts=[receipt['id']])
                self.assertEqual(execute(attach)['status'],'lessons_attached')
                self.assertEqual(execute(submit)['status'],'answers_queued')

    def test_job_verify_requires_every_command_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'report.json').write_text(json.dumps({'status':'awaiting_answers', 'commands':['mkdir'],
                'completed_commands':[]}), encoding='utf-8')
            args = argparse.Namespace(command='job', action='verify', job_id='synthetic')
            with patch('linuxstudio.job_path', return_value=root), \
                 patch('linuxstudio.current_report', return_value={'status':'awaiting_answers', 'commands':['mkdir'], 'completed_commands':[]}), \
                 patch('linuxstudio.authenticated') as auth:
                with self.assertRaises(RuntimeError):
                    execute(args)
                auth.assert_not_called()

    def test_duplicate_submit_keeps_original_batch_before_worker_consumes_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            report={'status':'awaiting_answers','run_id':'synthetic-run',
                'pending':[{'key':'mkdir:1:1','command':'mkdir','stage':1}]}
            control={'run_id':'synthetic-run','answers':{'mkdir:1:1':'mkdir first'}}
            (root/'report.json').write_text(json.dumps(report))
            (root/'control.json').write_text(json.dumps(control))
            original=(root/'control.json').read_bytes()
            answers=root/'answers.json';answers.write_text('{"mkdir:1:1":"mkdir second"}')
            args=argparse.Namespace(command='job',action='submit',job_id='synthetic',answers_file=answers)
            with patch('linuxstudio.job_path',return_value=root),patch('linuxstudio.current_report',return_value=report), \
                 patch('linuxstudio.authenticated') as auth:
                result=execute(args)
            self.assertEqual(result['status'],'waiting_for_worker')
            self.assertFalse(result['answers_queued'])
            self.assertEqual((root/'control.json').read_bytes(),original)
            self.assertFalse((root/'halt.json').exists())
            auth.assert_not_called()

    def test_submit_during_startup_or_post_does_not_halt_or_queue(self):
        for status in ('starting','submitting'):
            with self.subTest(status=status),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);report={'status':status}
                (root/'report.json').write_text(json.dumps(report))
                args=argparse.Namespace(command='job',action='submit',job_id='synthetic',answers_file=root/'unused.json')
                with patch('linuxstudio.job_path',return_value=root),patch('linuxstudio.current_report',return_value=report):
                    self.assertEqual(execute(args)['status'],'waiting_for_worker')
                self.assertFalse((root/'control.json').exists())
                self.assertFalse((root/'halt.json').exists())

    def test_terminal_submit_preserves_unknown_outcome_and_review(self):
        for status in ('complete','partial','error','cancelled'):
            with self.subTest(status=status),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);report={'status':status,'requires_user_review':status=='error',
                    'submission_outcome_unknown':status=='error'}
                (root/'report.json').write_text(json.dumps(report))
                original=(root/'report.json').read_bytes()
                args=argparse.Namespace(command='job',action='submit',job_id='synthetic',answers_file=root/'unused.json')
                with patch('linuxstudio.job_path',return_value=root),patch('linuxstudio.current_report',return_value=report), \
                     patch('linuxstudio.authenticated') as auth:
                    result=execute(args)
                self.assertEqual(result['status'],'job_finished')
                self.assertEqual(result['job_status'],status)
                self.assertEqual(result['requires_user_review'],status=='error')
                self.assertEqual((root/'report.json').read_bytes(),original)
                self.assertFalse((root/'control.json').exists())
                auth.assert_not_called()

    def test_stale_or_malformed_answers_still_halt_a_ready_job(self):
        for text in ('{broken','{"mkdir:1:1":"mkdir stale"}'):
            with self.subTest(text=text),tempfile.TemporaryDirectory() as temp:
                root=Path(temp)
                report={'status':'awaiting_answers','run_id':'synthetic-run',
                    'pending':[{'key':'mkdir:2:1','command':'mkdir','stage':2}]}
                (root/'report.json').write_text(json.dumps(report))
                answers=root/'answers.json';answers.write_text(text)
                args=argparse.Namespace(command='job',action='submit',job_id='synthetic',answers_file=answers)
                with patch('linuxstudio.job_path',return_value=root),patch('linuxstudio.current_report',return_value=report):
                    with self.assertRaises(ValueError):execute(args)
                self.assertEqual(json.loads((root/'halt.json').read_text())['category'],'invalid_answer_file_or_state')
                self.assertFalse((root/'control.json').exists())

    def test_fresh_next_round_can_replace_consumed_control(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            report={'status':'awaiting_answers','run_id':'synthetic-run','command_error_counts':{'mkdir':0},
                'pending':[{'key':'mkdir:2:1','command':'mkdir','stage':2}]}
            (root/'report.json').write_text(json.dumps(report))
            (root/'control.json').write_text(json.dumps({'run_id':'synthetic-run','answers':{'mkdir:1:1':'mkdir first'}}))
            answers=root/'answers.json';answers.write_text('{"mkdir:2:1":"mkdir next"}')
            args=argparse.Namespace(command='job',action='submit',job_id='synthetic',answers_file=answers)
            with patch('linuxstudio.job_path',return_value=root),patch('linuxstudio.current_report',return_value=report):
                self.assertEqual(execute(args)['status'],'answers_queued')
            self.assertEqual(json.loads((root/'control.json').read_text())['answers'],{'mkdir:2:1':'mkdir next'})

    def test_pending_hides_unconsumed_keys_and_waits_locally_for_next_round(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            initial={'status':'awaiting_answers','run_id':'synthetic-run',
                'pending':[{'key':'mkdir:1:1','command':'mkdir','stage':1}]}
            (root/'control.json').write_text(json.dumps({'run_id':'synthetic-run','answers':{'mkdir:1:1':'mkdir first'}}))
            args=argparse.Namespace(command='job',action='pending',job_id='synthetic',wait=0)
            with patch('linuxstudio.job_path',return_value=root),patch('linuxstudio.current_report',return_value=initial):
                result=execute(args)
            self.assertEqual(result['status'],'answers_queued')
            self.assertEqual(result['pending'],[])
            following={**initial,'pending':[{'key':'mkdir:2:1','command':'mkdir','stage':2}]}
            args.wait=1
            with patch('linuxstudio.job_path',return_value=root), \
                 patch('linuxstudio.current_report',side_effect=[initial,{'status':'submitting'},following]), \
                 patch('linuxstudio.time.sleep'),patch('linuxstudio.authenticated') as auth:
                result=execute(args)
            self.assertFalse(result['submission_in_progress'])
            self.assertEqual(result['pending'][0]['key'],'mkdir:2:1')
            auth.assert_not_called()

    def test_pending_wait_is_bounded_when_worker_stays_busy(self):
        with tempfile.TemporaryDirectory() as temp:
            args=argparse.Namespace(command='job',action='pending',job_id='synthetic',wait=3)
            with patch('linuxstudio.job_path',return_value=Path(temp)), \
                 patch('linuxstudio.current_report',return_value={'status':'submitting'}), \
                 patch('linuxstudio.time.monotonic',side_effect=[0,0,0,3]), \
                 patch('linuxstudio.time.sleep') as sleep:
                result=execute(args)
            self.assertTrue(result['submission_in_progress'])
            self.assertEqual(result['pending'],[])
            sleep.assert_called_once_with(0.1)

    def test_pending_feedback_is_only_the_current_rejection(self):
        from linuxstudio import pending_snapshot
        report={'status':'awaiting_answers','run_id':'synthetic-run','worker_version':'synthetic-version',
            'pending':[{'key':'mkdir:2:3','command':'mkdir','stage':2},
                       {'key':'head:2:1','command':'head','stage':2}],
            'submissions':[{'command':'head','stage':1,'attempt':1,'passed':False,'answer':'old','feedback':'old error'},
                           {'command':'mkdir','stage':2,'attempt':1,'passed':False,'answer':'first','feedback':'first error'},
                           {'command':'mkdir','stage':2,'attempt':2,'passed':False,'answer':'latest','feedback':'use parent path'}]}
        with tempfile.TemporaryDirectory() as temp:
            result=pending_snapshot(Path(temp),report)
        self.assertEqual(result['pending'][0]['last_rejection'],
            {'attempt':2,'answer':'latest','feedback':'use parent path'})
        self.assertNotIn('last_rejection',result['pending'][1])
        self.assertNotIn('last_rejection',report['pending'][0])
        self.assertEqual(result['worker_version'],'synthetic-version')

    def test_compact_status_keeps_outcome_without_full_feedback_or_tutorials(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            report={'status':'partial','username':'synthetic','worker_version':'synthetic-version',
                'commands':['mkdir','head'],'requested_command_count':2,'completed_commands':['head'],
                'error_count':3,'pending':[], 'submissions':[{'feedback':'large historical response'}],
                'lesson_evidence':[{'sha256':'synthetic-hash'}],
                'skipped_commands':[{'command':'mkdir','stage':2,'error_count':3,'reason':'budget',
                                     'feedback':'large rejection','task':'old task'}]}
            args=argparse.Namespace(command='job',action='status',job_id='synthetic',compact=True)
            with patch('linuxstudio.job_path',return_value=root),patch('linuxstudio.current_report',return_value=report):
                result=execute(args)
                args.compact=False
                self.assertEqual(execute(args),report)
            self.assertEqual(result['completed_commands'],['head'])
            self.assertEqual(result['skipped_commands'],[{'command':'mkdir','stage':2,'error_count':3,'reason':'budget'}])
            self.assertNotIn('submissions',result)
            self.assertNotIn('lesson_evidence',result)


if __name__ == '__main__':
    unittest.main()
