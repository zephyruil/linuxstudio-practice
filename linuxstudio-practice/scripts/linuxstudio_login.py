"""Local account picker and Windows-user-encrypted credentials for LinuxStudio.

Run this file to sign in without submitting any practice answers. Credentials
are saved only after a successful login and an explicit checked save option.
"""
import argparse
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import tempfile

import requests
from bs4 import BeautifulSoup

BASE = 'http://www.linuxstudio.cn'
from runtime_paths import PRIVATE_DIR
STORE_PATH = PRIVATE_DIR / 'accounts.dpapi'


class LoginCancelled(RuntimeError):
    pass


class CredentialStoreError(RuntimeError):
    pass


def _dpapi(data, decrypt=False):
    if os.name != 'nt':
        raise CredentialStoreError('Account saving requires Windows DPAPI.')

    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p if decrypt else wintypes.LPCWSTR,
                         ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    # Current Windows user scope; deliberately never use LOCAL_MACHINE.
    ok = function(ctypes.byref(source), None if decrypt else 'LinuxStudio accounts',
                  None, None, None, 0x01, ctypes.byref(result))
    if not ok:
        raise CredentialStoreError('Windows could not decrypt the account file.' if decrypt
                                   else 'Windows could not encrypt the account file.')
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        if decrypt and result.pbData:
            ctypes.memset(result.pbData, 0, result.cbData)
        kernel.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


class AccountStore:
    def __init__(self, path=STORE_PATH):
        self.path = Path(path)

    def _read(self):
        if not self.path.exists():
            return {}
        try:
            record = json.loads(_dpapi(self.path.read_bytes(), decrypt=True).decode('utf-8'))
            if record.get('version') != 1 or record.get('site') != BASE:
                raise ValueError('Wrong account file schema or site')
            accounts = record['accounts']
            if not isinstance(accounts, dict) or any(
                    not isinstance(u, str) or not isinstance(p, str) for u, p in accounts.items()):
                raise ValueError('Invalid account file')
            return accounts
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CredentialStoreError('Cannot read saved accounts; the existing file was not changed.') from exc

    def _write(self, accounts):
        payload = json.dumps({'version': 1, 'site': BASE, 'accounts': accounts},
                             ensure_ascii=False).encode('utf-8')
        encrypted = _dpapi(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, suffix='.encrypted.tmp', delete=False) as f:
                temporary = Path(f.name)
                f.write(encrypted)
                f.flush()
                os.fsync(f.fileno())
            temporary.replace(self.path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def names(self):
        return sorted(self._read(), key=str.casefold)

    def password(self, username):
        try:
            return self._read()[username]
        except KeyError as exc:
            raise CredentialStoreError('This account is no longer saved.') from exc

    def save(self, username, password):
        if not username or not password:
            raise ValueError('Username and password must not be empty.')
        accounts = self._read()
        accounts[username] = password
        self._write(accounts)

    def remove(self, username):
        accounts = self._read()
        accounts.pop(username, None)
        self._write(accounts)


@dataclass
class Credentials:
    username: str
    password: str = field(repr=False)
    remember: bool = False
    from_saved: bool = False


@dataclass
class LoginResult:
    username: str
    password_saved: bool
    used_saved_account: bool
    session_reused: bool = False
    persistent_session: bool = False


def choose_credentials(initial_username=None, store=None, error=None, report_path=None):
    import tkinter as tk
    from tkinter import ttk, messagebox
    store = store or AccountStore()
    parent = tk._default_root
    root = tk.Toplevel(parent) if parent is not None else tk.Tk()
    root.title('LinuxStudio · 账号登录')
    root.resizable(False, False)
    root.attributes('-topmost', True)
    try:
        names = store.names()
    except CredentialStoreError:
        root.destroy()
        raise
    new_label = '＋ 使用新账号'
    panel = ttk.Frame(root, padding=22)
    panel.grid()
    ttk.Label(panel, text='登录 LinuxStudio', font=('Microsoft YaHei UI', 15)).grid(row=0, column=0, columnspan=2, sticky='w')
    ttk.Label(panel, text='www.linuxstudio.cn', foreground='#666666').grid(row=1, column=0, columnspan=2, sticky='w', pady=(3, 16))
    ttk.Label(panel, text='账号选择').grid(row=2, column=0, sticky='w', padx=(0, 16))
    selected = tk.StringVar(master=root, value=initial_username if initial_username in names else new_label)
    picker = ttk.Combobox(panel, state='readonly', width=34, textvariable=selected, values=[new_label, *names])
    picker.grid(row=2, column=1, sticky='ew', pady=5)
    username = tk.StringVar(master=root, value=initial_username or '')
    password = tk.StringVar(master=root)
    use_saved = tk.BooleanVar(master=root, value=initial_username in names and not error)
    remember = tk.BooleanVar(master=root, value=False)
    ttk.Label(panel, text='用户名').grid(row=3, column=0, sticky='w')
    user_entry = ttk.Entry(panel, textvariable=username, width=36)
    user_entry.grid(row=3, column=1, sticky='ew', pady=5)
    ttk.Label(panel, text='密码').grid(row=4, column=0, sticky='w')
    password_entry = ttk.Entry(panel, textvariable=password, show='●', width=36)
    password_entry.grid(row=4, column=1, sticky='ew', pady=5)
    saved_check = ttk.Checkbutton(panel, text='使用此账号已保存的密码', variable=use_saved)
    saved_check.grid(row=5, column=1, sticky='w', pady=4)
    ttk.Checkbutton(panel, text='登录成功后保存本次账号和密码', variable=remember).grid(row=6, column=1, sticky='w', pady=4)
    ttk.Label(panel, text='仅保存在本机，并由当前 Windows 用户加密保护。',
              foreground='#666666').grid(row=7, column=0, columnspan=2, sticky='w', pady=(10, 5))
    status = tk.StringVar(master=root, value=error or '')
    ttk.Label(panel, textvariable=status, foreground='#b42318', wraplength=390).grid(row=8, column=0, columnspan=2, sticky='w')
    result = []

    def update_password_state():
        password_entry.configure(state='disabled' if use_saved.get() else 'normal')

    def select_account(event=None, first=False):
        existing = selected.get() in names
        if existing:
            username.set(selected.get())
        elif not first:
            username.set('')
        password.set('')
        user_entry.configure(state='readonly' if existing else 'normal')
        saved_check.configure(state='normal' if existing else 'disabled')
        use_saved.set(existing and not (first and error))
        remember.set(False)
        update_password_state()
        if not existing:
            user_entry.focus_set()
        elif not use_saved.get():
            password_entry.focus_set()

    def confirm(event=None):
        name = username.get().strip()
        if not name:
            status.set('请输入用户名。')
            return
        try:
            secret = store.password(name) if use_saved.get() else password.get()
        except CredentialStoreError:
            status.set('无法读取已保存密码，请重新输入。')
            use_saved.set(False)
            update_password_state()
            return
        if not secret:
            status.set('请输入密码，或选择使用已保存密码。')
            password_entry.focus_set()
            return
        result.append(Credentials(name, secret, remember.get(), use_saved.get()))
        password.set('')
        root.destroy()

    def forget():
        name = selected.get()
        if name not in names:
            return
        if messagebox.askyesno('移除已保存账号', f'移除本机保存的 {name} 及其密码？', parent=root):
            try:
                store.remove(name)
            except CredentialStoreError:
                status.set('移除失败，保存文件未更改。')
                return
            names.remove(name)
            picker.configure(values=[new_label, *names])
            selected.set(new_label)
            select_account()

    buttons = ttk.Frame(panel)
    buttons.grid(row=9, column=0, columnspan=2, sticky='ew', pady=(16, 0))
    ttk.Button(buttons, text='移除已保存账号', command=forget).pack(side='left')
    ttk.Button(buttons, text='取消', command=root.destroy).pack(side='right', padx=(8, 0))
    ttk.Button(buttons, text='登录', command=confirm).pack(side='right')
    picker.bind('<<ComboboxSelected>>', select_account)
    saved_check.configure(command=update_password_state)
    root.bind('<Return>', confirm)
    root.bind('<Escape>', lambda event: root.destroy())
    select_account(first=True)
    root.update_idletasks()
    root.geometry(f'+{max(0, (root.winfo_screenwidth()-root.winfo_width())//2)}+{max(0, (root.winfo_screenheight()-root.winfo_height())//2)}')
    if report_path:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(json.dumps({'status': 'awaiting_account_selection',
            'suggested_username': initial_username, 'saved_account_count': len(names)}, ensure_ascii=False), encoding='utf-8')
    # Wait for this window only. A nested Tk.mainloop() would keep waiting for
    # the persistent owner window after the user has closed the account picker.
    root.wait_window()
    if not result:
        raise LoginCancelled('Local login cancelled; no exercise submitted.')
    return result[0]


def _soup(response):
    response.raise_for_status()
    if urlsplit_host(response.url) != 'www.linuxstudio.cn':
        raise RuntimeError('Unexpected login redirect.')
    response.encoding = 'utf-8'
    return BeautifulSoup(response.text, 'html.parser')


def urlsplit_host(url):
    from urllib.parse import urlsplit
    return urlsplit(url).hostname


def _attempt_login(session, choice):
    from urllib.parse import urljoin
    session.cookies.clear()
    response = session.get(BASE + '/user/index.php', timeout=(10, 20))
    form = _soup(response).find('form', attrs={'name': 'LoginForm'})
    if form is None:
        raise RuntimeError('Login form not found.')
    target = urljoin(response.url, form.get('action', ''))
    if not target.startswith(BASE + '/') or form.get('method', '').lower() != 'post':
        raise RuntimeError('Unexpected login form.')
    payload = {e['name']: e.get('value', '') for e in form.select('input[name]')
               if e.get('type', 'text').lower() in ('hidden', 'submit')}
    payload.update(username=choice.username, password=choice.password)
    try:
        result = session.post(target, data=payload, timeout=(10, 20), allow_redirects=False)
        result.request.body = None
        result.raise_for_status()
    finally:
        payload.pop('password', None)
    page = _soup(session.get(BASE + '/user/my_practice.php', timeout=(10, 20)))
    return (page.select_one('a[href="/user/login.php?action=logout"]') is not None
            and re.search(r'(?:^|\s)' + re.escape(choice.username) + r'\s*已登录',
                          page.get_text(' ', strip=True)) is not None)


def login_direct(session, initial_username=None, report_path=None, store=None,
                 chooser=None, attempts=3):
    store = store or AccountStore()
    chooser = chooser or choose_credentials
    error = None
    for _ in range(attempts):
        choice = chooser(initial_username=initial_username, store=store, error=error, report_path=report_path)
        try:
            if not _attempt_login(session, choice):
                initial_username = choice.username
                error = '网站未确认登录成功，请重新输入密码或更换账号。'
                continue
            if choice.remember:
                store.save(choice.username, choice.password)
            saved_current_password = (choice.username in store.names()
                                      and store.password(choice.username) == choice.password)
            result = LoginResult(choice.username, saved_current_password, choice.from_saved)
            return result
        finally:
            choice.password = ''
    raise RuntimeError('Login was not confirmed; no exercise submitted.')


def login_interactive(session, initial_username=None, report_path=None, store=None,
                      chooser=None, attempts=3):
    # Explicit injected stores/choosers remain isolated for offline tests.
    if store is not None or chooser is not None:
        return login_direct(session, initial_username, report_path, store, chooser, attempts)
    from linuxstudio_session import attach_session
    return attach_session(session, initial_username, report_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--login', help='Optional suggested account; user may select another account.')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    try:
        with requests.Session() as session:
            result = login_interactive(session, args.login, args.report)
        from linuxstudio_session import start_window
        start_window(show=True)
        report = {'status': 'login_verified', 'username': result.username,
                  'authenticated': True, 'password_saved': result.password_saved,
                  'used_saved_account': result.used_saved_account,
                  'session_reused': result.session_reused,
                  'persistent_session': result.persistent_session,
                  'practice_submitted': False}
    except Exception as exc:
        report = {'status': 'cancelled' if isinstance(exc, LoginCancelled) else 'error', 'error': str(exc)}
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
