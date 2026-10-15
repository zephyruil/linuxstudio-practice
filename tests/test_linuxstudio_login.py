"""Credential-specific regression checks using synthetic accounts only."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'linuxstudio-practice' / 'scripts'))
from linuxstudio_login import (AccountStore, Credentials, CredentialStoreError,
                              LoginCancelled, login_interactive, _attempt_login)


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'accounts.dpapi'
        self.store = AccountStore(self.path)

    def test_encrypted_multi_account_roundtrip_update_and_remove(self):
        self.store.save('synthetic_a', 'not-a-real-password-甲')
        self.store.save('synthetic_b', 'not-a-real-password-乙')
        encrypted = self.path.read_bytes()
        for secret in ('synthetic_a', 'synthetic_b', 'not-a-real-password'):
            self.assertNotIn(secret.encode(), encrypted)
        loaded = AccountStore(self.path)
        self.assertEqual(loaded.names(), ['synthetic_a', 'synthetic_b'])
        self.assertEqual(loaded.password('synthetic_a'), 'not-a-real-password-甲')
        loaded.save('synthetic_a', 'updated-synthetic-password')
        self.assertEqual(loaded.password('synthetic_a'), 'updated-synthetic-password')
        loaded.remove('synthetic_a')
        self.assertEqual(loaded.names(), ['synthetic_b'])

    def test_corrupted_store_fails_without_overwriting(self):
        self.store.save('synthetic', 'synthetic-test-secret')
        data = bytearray(self.path.read_bytes())
        data[-1] ^= 1
        self.path.write_bytes(data)
        with self.assertRaises(CredentialStoreError):
            self.store.save('other', 'other-synthetic-secret')
        self.assertEqual(self.path.read_bytes(), data)

    def test_unchecked_save_does_not_create_file(self):
        choice = Credentials('synthetic', 'synthetic-test-secret', remember=False)
        with patch('linuxstudio_login._attempt_login', return_value=True):
            result = login_interactive(object(), store=self.store, chooser=lambda **kwargs: choice)
        self.assertFalse(result.password_saved)
        self.assertFalse(self.path.exists())
        self.assertEqual(choice.password, '')

    def test_failed_password_not_saved_and_user_can_change_account(self):
        choices = [Credentials('wrong_account', 'wrong-synthetic-secret', remember=True),
                   Credentials('new_account', 'correct-synthetic-secret', remember=True)]
        def choose(**kwargs):
            return choices.pop(0)
        with patch('linuxstudio_login._attempt_login', side_effect=[False, True]):
            result = login_interactive(object(), 'initial_hint', store=self.store, chooser=choose)
        self.assertEqual(result.username, 'new_account')
        self.assertEqual(self.store.names(), ['new_account'])
        self.assertEqual(self.store.password('new_account'), 'correct-synthetic-secret')

    def test_saved_account_login_without_reentering_password(self):
        self.store.save('saved_user', 'synthetic-saved-secret')
        choice = Credentials('saved_user', self.store.password('saved_user'), from_saved=True)
        with patch('linuxstudio_login._attempt_login', return_value=True):
            result = login_interactive(object(), store=self.store, chooser=lambda **kwargs: choice)
        self.assertTrue(result.password_saved)
        self.assertTrue(result.used_saved_account)

    def test_cancel_never_authenticates_or_saves(self):
        def cancelled(**kwargs):
            raise LoginCancelled('cancelled')
        with patch('linuxstudio_login._attempt_login') as attempt:
            with self.assertRaises(LoginCancelled):
                login_interactive(object(), store=self.store, chooser=cancelled)
            attempt.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_credentials_repr_hides_password(self):
        self.assertNotIn('synthetic-test-secret', repr(Credentials('user', 'synthetic-test-secret')))

    def test_auth_request_checks_selected_identity_and_drops_encoded_body(self):
        class Response:
            url = 'http://www.linuxstudio.cn/user/index.php'
            encoding = None
            def __init__(self, text):
                self.text = text
                self.request = type('Request', (), {'body': 'synthetic-encoded-body'})()
            def raise_for_status(self):
                pass
        class Cookies:
            def clear(self):
                pass
        class Session:
            cookies = Cookies()
            def get(self, url, **kwargs):
                if url.endswith('index.php'):
                    return Response('<form name="LoginForm" action="login.php" method="post"><input name="username"><input type="password" name="password"><input type="submit" name="submit" value="ok"></form>')
                return Response('selected_user 已登录 <a href="/user/login.php?action=logout">退出</a>')
            def post(self, url, data, **kwargs):
                self.posted_username = data['username']
                self.posted_password = data['password']
                self.last_response = Response('ok')
                return self.last_response
        session = Session()
        self.assertTrue(_attempt_login(session, Credentials('selected_user', 'synthetic-secret')))
        self.assertEqual(session.posted_username, 'selected_user')
        self.assertIsNone(session.last_response.request.body)
        self.assertFalse(_attempt_login(session, Credentials('different_user', 'synthetic-secret')))
        self.assertFalse(_attempt_login(session, Credentials('user', 'synthetic-secret')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
