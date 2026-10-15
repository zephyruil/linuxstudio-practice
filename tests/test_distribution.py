"""Clean archive, isolated installation, upgrade and noninteractive CLI tests."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parent.parent


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class DistributionTests(unittest.TestCase):
    def test_clean_archive_installs_and_upgrades_without_runtime_data(self):
        builder = module('distribution_build', ROOT / 'tools/build.py')
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            built = builder.build(temp / 'dist')
            with zipfile.ZipFile(built['archive']) as archive:
                names = archive.namelist()
                self.assertEqual(len(names), len(builder.FILES) + 1)
                self.assertFalse(any(any(segment in name for segment in
                    ('__pycache__', '.dpapi', '/private/', '/jobs/', '.venv', '.git/')) for name in names))
                archive.extractall(temp / 'unpacked')
            unpacked = temp / 'unpacked' / Path(built['archive']).stem
            manifest = json.loads((unpacked / 'MANIFEST.json').read_text(encoding='utf-8'))
            for name, digest in manifest['files'].items():
                self.assertEqual(hashlib.sha256((unpacked / name).read_bytes()).hexdigest(), digest)
            destination = temp / 'test-agent/skills'
            state = temp / 'personal-test-state'
            state.mkdir()
            marker = state / 'keep.txt'
            marker.write_text('synthetic retained state')
            env = {**os.environ, 'PYTHONUTF8':'1', 'LINUXSTUDIO_DATA_DIR':str(state)}
            command = [sys.executable, str(unpacked / 'install.py'), '--skills-dir', str(destination), '--no-deps']
            installed = subprocess.run(command, env=env, capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(installed.returncode, 0, installed.stderr)
            skill = destination / 'linuxstudio-practice'
            entry = [sys.executable, str(skill / 'scripts/launcher.py')]
            doctor = subprocess.run([*entry, 'doctor'], env=env, capture_output=True, text=True, encoding='utf-8', check=True)
            self.assertTrue(json.loads(doctor.stdout)['supported'])
            self.assertEqual(json.loads(doctor.stdout)['version'], manifest['version'])
            self.assertEqual(json.loads(doctor.stdout)['data_dir'], str(state.resolve()))
            # Missing session: no UI, no account data created, immediate failure.
            reuse = subprocess.run([*entry, 'session', 'status', '--verify'], env=env,
                                   capture_output=True, text=True, encoding='utf-8', timeout=10)
            self.assertEqual(reuse.returncode, 1)
            self.assertIn('未弹出', json.loads(reuse.stdout)['error'])
            self.assertFalse((state / 'private/accounts.dpapi').exists())
            overwritten = subprocess.run(command, env=env, capture_output=True)
            self.assertNotEqual(overwritten.returncode, 0)
            upgraded = subprocess.run([*command, '--upgrade'], env=env, capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
            self.assertTrue(Path(json.loads(upgraded.stdout)['backup']).is_dir())
            self.assertEqual(marker.read_text(), 'synthetic retained state')

    def test_manifest_detects_modified_source(self):
        installer = module('distribution_install', ROOT / 'install.py')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'source.py').write_text('original')
            manifest = {'files': {'source.py': hashlib.sha256(b'original').hexdigest()}}
            (root / 'MANIFEST.json').write_text(json.dumps(manifest))
            installer.verify_package(root)
            (root / 'source.py').write_text('changed')
            with self.assertRaises(RuntimeError):
                installer.verify_package(root)

    def test_real_cli_duplicate_submit_and_pending_do_not_use_login_or_http(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            job_id='00000000-0000-4000-8000-000000000001'
            directory=root/'jobs'/job_id;directory.mkdir(parents=True)
            report={'status':'awaiting_answers','username':'synthetic','run_id':'synthetic-run',
                'commands':['mkdir'],'command_error_counts':{'mkdir':0},
                'pending':[{'key':'mkdir:1:1','command':'mkdir','stage':1,'total':2,'task':'synthetic task'}]}
            (directory/'report.json').write_text(json.dumps(report))
            (directory/'worker.json').write_text(json.dumps({'pid':os.getpid()}))
            answers=root/'answers.json';answers.write_text('{"mkdir:1:1":"mkdir synthetic"}')
            env={**os.environ,'PYTHONUTF8':'1','LINUXSTUDIO_DATA_DIR':str(root)}
            entry=[sys.executable,str(ROOT/'linuxstudio-practice/scripts/launcher.py')]
            def call(*args,expected_code=0):
                result=subprocess.run([*entry,*args],env=env,capture_output=True,text=True,encoding='utf-8',timeout=10)
                self.assertEqual(result.returncode,expected_code,result.stdout+result.stderr)
                return json.loads(result.stdout)
            self.assertEqual(call('job','submit',job_id,'--answers-file',str(answers))['status'],'answers_queued')
            original=(directory/'control.json').read_bytes()
            self.assertEqual(call('job','submit',job_id,'--answers-file',str(answers))['status'],'waiting_for_worker')
            self.assertEqual((directory/'control.json').read_bytes(),original)
            pending=call('job','pending',job_id,'--wait','0')
            self.assertEqual(pending['status'],'answers_queued')
            self.assertEqual(pending['pending'],[])
            self.assertEqual(call('job','status',job_id,'--compact')['pending_count'],1)
            for value in ('-1','31','nan','inf'):
                result=subprocess.run([*entry,'job','pending',job_id,'--wait',value],env=env,capture_output=True,timeout=10)
                self.assertEqual(result.returncode,2)
            self.assertFalse((directory/'halt.json').exists())
            self.assertFalse((root/'private').exists())


if __name__ == '__main__':
    unittest.main()
