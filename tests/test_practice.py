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
from linuxstudio import job_path, validated_answers, execute
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
    def __init__(self, lit=True, wrong_account=False, reject=False, timeout=False):
        self.headers = {}
        self.posts = []
        self.lit, self.wrong_account, self.reject, self.timeout = lit, wrong_account, reject, timeout
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def get(self, url, **kwargs):
        if 'json_data' in url:
            return Response(data={'data': [{'name': 'mkdir', 'value': 1}]})
        if 'json_color' in url:
            return Response(data=['#FE8463' if self.lit and len(self.posts) >= 2 else '#fcc3b4'])
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
    def run_worker(self, site):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            catalog = root / 'catalog.json'
            catalog.write_text('{"mkdir":"1_60_mkdir"}', encoding='utf-8')
            report, control = root / 'report.json', root / 'control.json'
            from linuxstudio_common import write_report
            def answer_pending(path, value):
                write_report(path, value)
                if value['status'] == 'awaiting_answers':
                    control.write_text(json.dumps({'run_id': value['run_id'],
                        'answers': {p['key']: 'mkdir synthetic_dir' for p in value['pending']}}), encoding='utf-8')
            argv = ['worker', '--reuse-only', '--login', 'synthetic', '--commands', 'mkdir',
                    '--catalog-file', str(catalog), '--report', str(report), '--control-file', str(control)]
            with patch.object(sys, 'argv', argv), patch.object(worker, 'TimedSession', return_value=site), \
                 patch('linuxstudio_session.attach_session', return_value=LoginResult('synthetic', False, False)), \
                 patch.object(worker, 'write_report', side_effect=answer_pending):
                code = worker.main()
            return code, json.loads(report.read_text(encoding='utf-8'))

    def test_all_stages_real_fields_and_account_progress(self):
        site = Site()
        code, report = self.run_worker(site)
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['total_stages_passed'], 2)
        self.assertTrue(report['account_progress_verified'])
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

    def test_rejection_is_bounded_and_not_completion(self):
        site = Site(reject=True)
        code, report = self.run_worker(site)
        self.assertEqual(code, 1)
        self.assertEqual(len(site.posts), 5)
        self.assertFalse(any(p['passed'] for p in report['submissions']))

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


if __name__ == '__main__':
    unittest.main()
