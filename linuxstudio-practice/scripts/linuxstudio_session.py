"""User-controlled persistent LinuxStudio session window.

Only this application's own HTTP cookies are retained, encrypted with the
current Windows user's DPAPI. Browser profiles and browser cookies are unused.
Every attached HTTP client checks the current session generation before sending
requests, so explicit logout or account switching invalidates old clients.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import requests
from requests.adapters import HTTPAdapter
from requests.cookies import create_cookie

from linuxstudio_login import (BASE, STORE_PATH, LoginCancelled, LoginResult,
                               _dpapi, _soup, login_direct)

STATE_DIR = STORE_PATH.parent
SESSION_PATH = STATE_DIR / 'active_session.dpapi'
WINDOW_PATH = STATE_DIR / 'session_window.json'
SHOW_PATH = STATE_DIR / 'show_session_window.json'
LOGOUT_PATH = STATE_DIR / 'logout_session_window.json'


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.tmp', delete=False) as f:
            temporary = Path(f.name)
            f.write(data)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


class SessionStore:
    def __init__(self, path=SESSION_PATH):
        self.path = Path(path)

    @contextmanager
    def locked(self):
        import msvcrt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix('.lock')
        with lock_path.open('a+b') as f:
            if f.tell() == 0:
                f.write(b'0')
                f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)

    def _read(self):
        if not self.path.exists():
            return None
        record = json.loads(_dpapi(self.path.read_bytes(), decrypt=True).decode('utf-8'))
        if record.get('version') != 1 or record.get('site') != BASE:
            raise RuntimeError('Saved session file has an unexpected format.')
        return record

    def load(self):
        with self.locked():
            return self._read()

    def save(self, session, auth):
        record = {'version': 1, 'site': BASE, 'active': True,
                  'id': str(uuid.uuid4()), 'username': auth.username,
                  'password_saved': auth.password_saved,
                  'used_saved_account': auth.used_saved_account,
                  'cookies': [{'name': c.name, 'value': c.value, 'domain': c.domain,
                               'path': c.path, 'secure': c.secure, 'expires': c.expires,
                               'rest': dict(c._rest)} for c in session.cookies]}
        with self.locked():
            atomic_write(self.path, _dpapi(json.dumps(record, ensure_ascii=False).encode('utf-8')))
        return record

    def invalidate(self, session_id):
        with self.locked():
            record = self._read()
            if record is not None and record.get('id') == session_id:
                # Logout erases cookies from the encrypted record, retaining only
                # a generation tombstone to reject stale background clients.
                record.update(active=False, cookies=[])
                atomic_write(self.path, _dpapi(json.dumps(record, ensure_ascii=False).encode('utf-8')))

    def require(self, session_id):
        record = self.load()
        if record is None or not record.get('active') or record.get('id') != session_id:
            raise requests.exceptions.ConnectionError('本机会话已退出或已切换账号；已阻止后续请求。')
        return record


def restore(session, record):
    session.cookies.clear()
    for cookie in record['cookies']:
        if cookie['domain'].lstrip('.') not in ('www.linuxstudio.cn', 'linuxstudio.cn'):
            raise RuntimeError('Saved session contains an unexpected cookie domain.')
        session.cookies.set_cookie(create_cookie(**cookie))


def identity_ok(session, username):
    import re
    page = _soup(session.get(BASE + '/user/my_practice.php', timeout=(10, 20)))
    return (page.select_one('a[href="/user/login.php?action=logout"]') is not None
            and re.search(r'(?:^|\s)' + re.escape(username) + r'\s*已登录', page.get_text(' ', strip=True)) is not None)


class SessionGuard(HTTPAdapter):
    def __init__(self, store, session_id):
        super().__init__()
        self.store, self.session_id = store, session_id

    def send(self, request, **kwargs):
        self.store.require(self.session_id)
        return super().send(request, **kwargs)


def window_status():
    try:
        return json.loads(WINDOW_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def window_running():
    status = window_status()
    if status.get('status') == 'closed' or not isinstance(status.get('pid'), int):
        return False
    return process_running(status['pid'])


def process_running(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    import ctypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        return False
    try:
        return kernel.WaitForSingleObject(handle, 0) == 258
    finally:
        kernel.CloseHandle(handle)


def start_window(initial_username=None, show=False):
    if window_running():
        if show:
            atomic_write(SHOW_PATH, json.dumps({'id': str(uuid.uuid4())}).encode())
        return None
    interpreter = Path(sys.executable)
    if interpreter.name.lower() == 'python.exe' and interpreter.with_name('pythonw.exe').exists():
        interpreter = interpreter.with_name('pythonw.exe')
    command = [str(interpreter), str(Path(__file__).resolve())]
    if initial_username:
        command.extend(['--login', initial_username])
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)


def attach_session(session, initial_username=None, report_path=None, allow_prompt=True):
    store = SessionStore()
    if not allow_prompt and not window_running():
        raise RuntimeError('当前没有可复用的登录窗口；按要求未弹出登录窗口。')
    process = start_window(initial_username) if allow_prompt else None
    deadline = time.monotonic() + 900
    candidate = None
    shown = False
    while time.monotonic() < deadline:
        status = window_status()
        if status.get('status') == 'cancelled':
            raise LoginCancelled('已取消本次登录；未提交练习。')
        candidate = store.load()
        if (candidate and candidate.get('active') and status.get('status') == 'authenticated'
                and status.get('session_id') == candidate['id'] and window_running()):
            if initial_username and candidate['username'] != initial_username and process is None:
                if not allow_prompt:
                    raise RuntimeError('当前登录账号与任务账号不一致；按要求未弹出登录窗口。')
                if not shown:
                    start_window(show=True)
                    shown = True
                if report_path:
                    atomic_write(report_path, json.dumps({'status': 'awaiting_account_switch',
                        'requested_username': initial_username, 'active_username': candidate['username']}, ensure_ascii=False).encode('utf-8'))
                time.sleep(0.3)
                continue
            break
        if status.get('status') in ('expired', 'cancelled', 'error') and not shown:
            if not allow_prompt:
                raise RuntimeError('当前会话不可复用；按要求未弹出登录窗口。')
            start_window(show=True)
            shown = True
        if process is not None and process.poll() is not None:
            raise LoginCancelled('会话窗口已关闭或登录已取消；未提交练习。')
        if not allow_prompt:
            raise RuntimeError('当前会话尚未登录；按要求未弹出登录窗口。')
        time.sleep(0.2)
    else:
        raise RuntimeError('等待用户确认登录超时。')
    restore(session, candidate)
    guard = SessionGuard(store, candidate['id'])
    session.mount(BASE + '/', guard)
    session.mount('https://www.linuxstudio.cn/', guard)
    if not identity_ok(session, candidate['username']):
        store.invalidate(candidate['id'])
        if allow_prompt:
            start_window(show=True)
        raise RuntimeError('网站会话已过期，请在本机会话窗口点击重新登录。')
    return LoginResult(candidate['username'], candidate['password_saved'],
                       candidate['used_saved_account'], session_reused=process is None,
                       persistent_session=True)


def main():
    import tkinter as tk
    from tkinter import ttk
    import ctypes
    import hashlib
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--login')
    args = parser.parse_args()
    # Only one owner window per local application directory and Windows session.
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    mutex_name = 'Local\\LinuxStudioSession_' + hashlib.sha256(str(SESSION_PATH).encode()).hexdigest()[:24]
    mutex = kernel.CreateMutexW(None, False, mutex_name)
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(mutex)
        atomic_write(SHOW_PATH, json.dumps({'id': str(uuid.uuid4())}).encode())
        return
    if not mutex:
        raise RuntimeError('Cannot create session window lock.')
    store = SessionStore()
    session = requests.Session()
    session.headers.update({'User-Agent': 'LinuxStudioPersistentSession/1.0', 'Cache-Control': 'no-cache'})
    root = tk.Tk()
    root.title('LinuxStudio · 登录会话')
    root.resizable(False, False)
    root.withdraw()
    panel = ttk.Frame(root, padding=24)
    panel.grid()
    ttk.Label(panel, text='LinuxStudio 登录会话', font=('Microsoft YaHei UI', 15)).grid(row=0, column=0, columnspan=3, sticky='w')
    username = tk.StringVar(master=root, value='尚未登录')
    state = tk.StringVar(master=root, value='等待确认登录')
    ttk.Label(panel, textvariable=username, font=('Microsoft YaHei UI', 12)).grid(row=1, column=0, columnspan=3, sticky='w', pady=(16, 6))
    ttk.Label(panel, textvariable=state, wraplength=430).grid(row=2, column=0, columnspan=3, sticky='w', pady=(0, 12))
    ttk.Label(panel, text='后续任务复用本次登录。关闭按钮只会最小化窗口。\n点击“退出登录”后，后台任务不能继续使用此会话。',
              foreground='#666666').grid(row=3, column=0, columnspan=3, sticky='w', pady=(0, 18))
    record = None
    closing = False
    last_show = None
    last_check = 0
    checking = False
    network_lock = threading.RLock()

    def publish(status):
        atomic_write(WINDOW_PATH, json.dumps({'pid': os.getpid(), 'status': status,
            'username': record['username'] if record else None,
            'session_id': record['id'] if record else None,
            'heartbeat': time.time()}, ensure_ascii=False).encode('utf-8'))

    def do_login():
        nonlocal record, last_check
        if closing:
            return
        if record:
            store.invalidate(record['id'])
        root.withdraw()
        publish('logging_in')
        try:
            with network_lock:
                auth = login_direct(session, record['username'] if record else args.login)
                record = store.save(session, auth)
            username.set('当前账号：' + auth.username)
            state.set('已登录 · 后续任务无需再次确认登录')
            last_check = time.time()
            publish('authenticated')
        except LoginCancelled:
            state.set('登录已取消，可点击重新登录。')
            publish('cancelled')
        except Exception:
            state.set('登录未成功，请点击重新登录。')
            publish('error')
        root.deiconify()

    def logout():
        nonlocal closing
        if closing:
            return
        closing = True
        if record:
            store.invalidate(record['id'])
        state.set('正在退出登录…')
        publish('closed')
        finished = threading.Event()
        def finish():
            try:
                with network_lock:
                    session.get(BASE + '/user/login.php?action=logout', timeout=(5, 8))
            except requests.RequestException:
                pass
            finally:
                session.cookies.clear()
                session.close()
                finished.set()
        threading.Thread(target=finish, daemon=True).start()
        def wait_closed():
            if finished.is_set():
                root.destroy()
            else:
                root.after(100, wait_closed)
        wait_closed()

    ttk.Button(panel, text='最小化到任务栏', command=root.iconify).grid(row=4, column=0, padx=(0, 8))
    ttk.Button(panel, text='重新登录 / 切换账号', command=do_login).grid(row=4, column=1, padx=(0, 8))
    ttk.Button(panel, text='退出登录', command=logout).grid(row=4, column=2)
    root.protocol('WM_DELETE_WINDOW', root.iconify)

    def tick():
        nonlocal last_show, last_check, checking
        if closing:
            return
        try:
            action = json.loads(LOGOUT_PATH.read_text(encoding='utf-8'))
            if record and action.get('session_id') == record['id']:
                logout()
                return
        except (OSError, ValueError):
            pass
        try:
            show = json.loads(SHOW_PATH.read_text(encoding='utf-8')).get('id')
            if show != last_show:
                last_show = show
                root.deiconify()
        except (OSError, ValueError):
            pass
        active = False
        if record:
            current = store.load()
            active = bool(current and current.get('active') and current['id'] == record['id'])
        if active:
            publish('authenticated')
            if not checking and time.time() - last_check > 240:
                checking = True
                def keepalive():
                    nonlocal checking, last_check
                    try:
                        with network_lock:
                            if not identity_ok(session, record['username']):
                                store.invalidate(record['id'])
                    except requests.RequestException:
                        pass  # A temporary network failure does not discard login.
                    finally:
                        checking = False
                        last_check = time.time()
                threading.Thread(target=keepalive, daemon=True).start()
        elif record:
            state.set('会话已失效或已退出，请点击重新登录。')
            publish('expired')
        root.after(1500, tick)

    try:
        publish('starting')
        try:
            record = store.load()
            if record and record.get('active'):
                restore(session, record)
                if identity_ok(session, record['username']):
                    username.set('当前账号：' + record['username'])
                    state.set('已恢复已确认的登录 · 后续任务无需再次登录')
                    publish('authenticated')
                    last_check = time.time()
                    root.deiconify()
                else:
                    store.invalidate(record['id'])
                    do_login()
            else:
                record = None
                do_login()
        except Exception:
            state.set('恢复会话失败，请点击重新登录。')
            publish('error')
            root.deiconify()
        tick()
        root.mainloop()
    finally:
        kernel.CloseHandle(mutex)


if __name__ == '__main__':
    main()
