"""Persistent-session regressions. All cookies and accounts are synthetic."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'linuxstudio-practice' / 'scripts'))
import requests
from linuxstudio_login import LoginResult
from linuxstudio_session import SessionStore, SessionGuard, restore, attach_session, BASE, atomic_write


class PersistentSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'session.dpapi'
        self.store = SessionStore(self.path)
        self.owner = requests.Session()
        self.addCleanup(self.owner.close)
        self.owner.cookies.set('synthetic_session', 'not-a-real-session-secret', domain='www.linuxstudio.cn', path='/')
        self.auth = LoginResult('synthetic_account', False, False)
        self.record = self.store.save(self.owner, self.auth)

    def test_only_ciphertext_is_persisted_and_cookie_can_restore(self):
        encrypted = self.path.read_bytes()
        self.assertNotIn(b'not-a-real-session-secret', encrypted)
        self.assertNotIn(b'synthetic_account', encrypted)
        with requests.Session() as client:
            restore(client, self.store.load())
            self.assertEqual(client.cookies.get('synthetic_session'), 'not-a-real-session-secret')

    def test_closing_task_session_keeps_owner_logged_in(self):
        with requests.Session() as client:
            restore(client, self.record)
            client.mount(BASE + '/', SessionGuard(self.store, self.record['id']))
        self.assertTrue(self.store.require(self.record['id'])['active'])

    def test_logout_removes_retained_cookies_and_blocks_old_requests(self):
        guard = SessionGuard(self.store, self.record['id'])
        self.store.invalidate(self.record['id'])
        self.assertFalse(self.store.load()['active'])
        self.assertEqual(self.store.load()['cookies'], [])
        request = requests.Request('POST', BASE + '/study/practice/practice.php', data={'ta_input': 'synthetic'}).prepare()
        with patch('requests.adapters.HTTPAdapter.send') as send:
            with self.assertRaises(requests.exceptions.ConnectionError):
                guard.send(request)
            send.assert_not_called()

    def test_account_switch_invalidates_previous_clients(self):
        old = self.record['id']
        new = self.store.save(self.owner, LoginResult('other_synthetic', False, False))
        self.assertNotEqual(old, new['id'])
        with self.assertRaises(requests.exceptions.ConnectionError):
            self.store.require(old)
        self.assertEqual(self.store.require(new['id'])['username'], 'other_synthetic')

    def test_stale_logout_cannot_erase_new_account(self):
        new = self.store.save(self.owner, LoginResult('other_synthetic', False, False))
        self.store.invalidate(self.record['id'])
        self.assertTrue(self.store.require(new['id'])['active'])

    def test_multiple_new_workers_reuse_without_password_dialog(self):
        status = {'status': 'authenticated', 'session_id': self.record['id']}
        with patch('linuxstudio_session.SessionStore', return_value=self.store), \
             patch('linuxstudio_session.start_window', return_value=None), \
             patch('linuxstudio_session.window_status', return_value=status), \
             patch('linuxstudio_session.window_running', return_value=True), \
             patch('linuxstudio_session.identity_ok', return_value=True) as check:
            for _ in range(2):
                with requests.Session() as client:
                    result = attach_session(client, 'synthetic_account')
                    self.assertTrue(result.persistent_session)
                    self.assertTrue(result.session_reused)
            self.assertEqual(check.call_count, 2)
        self.assertTrue(self.store.load()['active'])

    def test_expired_remote_session_does_not_silently_login(self):
        status = {'status': 'authenticated', 'session_id': self.record['id']}
        with patch('linuxstudio_session.SessionStore', return_value=self.store), \
             patch('linuxstudio_session.start_window', return_value=None), \
             patch('linuxstudio_session.window_status', return_value=status), \
             patch('linuxstudio_session.window_running', return_value=True), \
             patch('linuxstudio_session.identity_ok', return_value=False), \
             patch('linuxstudio_login.login_direct') as login:
            with requests.Session() as client:
                with self.assertRaises(RuntimeError):
                    attach_session(client, 'synthetic_account')
            login.assert_not_called()
        self.assertFalse(self.store.load()['active'])

    def test_account_picker_returns_with_owner_window_still_alive(self):
        import tkinter as tk
        from linuxstudio_login import AccountStore, LoginCancelled, choose_credentials
        owner = tk.Tk()
        owner.withdraw()
        original = tk.Toplevel
        def child(*args, **kwargs):
            window = original(*args, **kwargs)
            window.withdraw()
            window.after(100, window.destroy)
            return window
        try:
            with patch('tkinter.Toplevel', side_effect=child):
                with self.assertRaises(LoginCancelled):
                    choose_credentials(store=AccountStore(Path(self.temp.name) / 'accounts.dpapi'))
            self.assertTrue(owner.winfo_exists())
        finally:
            owner.destroy()

    def test_reuse_only_missing_window_never_starts_or_logs_in(self):
        with patch('linuxstudio_session.window_running', return_value=False), \
             patch('linuxstudio_session.start_window') as start:
            with self.assertRaises(RuntimeError):
                attach_session(self.owner, allow_prompt=False)
            start.assert_not_called()

    def test_reuse_only_account_mismatch_never_shows_picker(self):
        status = {'status': 'authenticated', 'session_id': self.record['id']}
        with patch('linuxstudio_session.SessionStore', return_value=self.store), \
             patch('linuxstudio_session.window_running', return_value=True), \
             patch('linuxstudio_session.window_status', return_value=status), \
             patch('linuxstudio_session.start_window') as start, \
             patch('linuxstudio_session.identity_ok') as identity:
            with self.assertRaises(RuntimeError):
                attach_session(self.owner, 'wrong_synthetic', allow_prompt=False)
            start.assert_not_called()
            identity.assert_not_called()

    def test_reuse_only_valid_session_never_starts_window(self):
        status = {'status': 'authenticated', 'session_id': self.record['id']}
        with patch('linuxstudio_session.SessionStore', return_value=self.store), \
             patch('linuxstudio_session.window_running', return_value=True), \
             patch('linuxstudio_session.window_status', return_value=status), \
             patch('linuxstudio_session.identity_ok', return_value=True), \
             patch('linuxstudio_session.start_window') as start:
            result = attach_session(self.owner, 'synthetic_account', allow_prompt=False)
            self.assertTrue(result.session_reused)
            start.assert_not_called()

    def test_atomic_write_recovers_after_windows_reader_releases_file(self):
        import ctypes
        import threading
        path = Path(self.temp.name) / 'control.json'
        path.write_bytes(b'old complete content')
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                      ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        kernel.CreateFileW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        # Permit reading/writing but not delete sharing, reproducing Windows'
        # denial of rename-over-open-file while a poller is reading it.
        handle = kernel.CreateFileW(str(path), 0x80000000, 0x3, None, 3, 0, None)
        self.assertNotIn(handle, (None, ctypes.c_void_p(-1).value))
        try:
            with self.assertRaises(PermissionError):
                candidate = path.with_suffix('.probe')
                candidate.write_bytes(b'probe')
                candidate.replace(path)
            candidate.unlink()
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        release = threading.Timer(0.15, lambda: kernel.CloseHandle(handle))
        release.start()
        try:
            atomic_write(path, b'new complete content')
        finally:
            release.join()
        self.assertEqual(path.read_bytes(), b'new complete content')
        self.assertEqual(list(path.parent.glob('*.tmp')), [])

    def test_atomic_write_permanent_denial_is_bounded_and_keeps_original(self):
        path = Path(self.temp.name) / 'control.json'
        path.write_bytes(b'original')
        with patch('pathlib.Path.replace', side_effect=PermissionError('synthetic locked destination')) as replace, \
             patch('linuxstudio_session.time.monotonic', side_effect=[0, 0.1, 10]), \
             patch('linuxstudio_session.time.sleep'):
            with self.assertRaises(PermissionError):
                atomic_write(path, b'replacement')
            self.assertEqual(replace.call_count, 2)
        self.assertEqual(path.read_bytes(), b'original')
        self.assertEqual(list(path.parent.glob('*.tmp')), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
