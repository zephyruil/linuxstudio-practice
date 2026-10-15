"""Shared site parsing; never interprets or executes practice answers locally."""
import json
import re
import hashlib
import uuid
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, parse_qs
from bs4 import BeautifulSoup

BASE = 'http://www.linuxstudio.cn'
VERSION = '1.4.0'
PROGRESS_NAMES = {
    'background_command': '前台后台相关命令：Ctrl+z、fg、bg、&、jobs',
    'zip': 'zip、unzip', 'gzip': 'gzip、gunzip', 'bzip2': 'bzip2、bunzip2',
    'pbzip2': 'pbzip2、pbunzip2', 'ftp': 'ftp、lftp', 'who_w': 'who、w',
    'pipeline': '管道',
}


def soup_for(response):
    response.raise_for_status()
    if urlsplit(response.url).netloc != 'www.linuxstudio.cn':
        raise RuntimeError('Unexpected site redirect.')
    response.encoding = 'utf-8'
    return BeautifulSoup(response.text, 'html.parser')


def verify_identity(page, username):
    if (page.select_one('a[href="/user/login.php?action=logout"]') is None or
        re.search(r'(?:^|\s)' + re.escape(username) + r'\s*已登录',
                  page.get_text(' ', strip=True)) is None):
        raise RuntimeError('Requested account is not authenticated; submission stopped.')


def write_report(path, report):
    from linuxstudio_session import atomic_write
    atomic_write(path, (json.dumps(report, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))


def parse_catalog(page):
    result = {}
    for link in page.select('a[href]'):
        url = urlsplit(urljoin(BASE + '/', link['href']))
        if url.netloc != 'www.linuxstudio.cn' or url.path != '/study/practice/practice.php':
            continue
        topic = parse_qs(url.query).get('topic', [''])[0]
        match = re.fullmatch(r'1_\d+_([A-Za-z0-9_]+)', topic)
        if not match:
            continue
        name = match[1]
        if name in result and result[name] != topic:
            raise RuntimeError('Ambiguous topic: ' + name)
        result[name] = topic
    if not result:
        raise RuntimeError('Linux command catalog not found; site format may have changed.')
    return result


def catalog(session):
    return parse_catalog(soup_for(session.get(BASE + '/practice.php',
        params={'chapter': 'Linux常用命令'}, timeout=(10, 20))))


def progress(session, topics):
    values = session.get(BASE + '/user/json_data_practice.php', timeout=(10, 20))
    colors = session.get(BASE + '/user/json_color_practice.php', timeout=(10, 20))
    values.raise_for_status()
    colors.raise_for_status()
    entries, color_list = values.json()['data'], colors.json()
    if not isinstance(entries, list) or not isinstance(color_list, list) or len(entries) != len(color_list):
        raise RuntimeError('Progress data and colors do not match.')
    by_name = {entry['name']: color_list[i] for i, entry in enumerate(entries)}
    result = {}
    for name in topics:
        label = PROGRESS_NAMES.get(name, name)
        if label in by_name:
            color = by_name[label]
            result[name] = {'color': color, 'completed': color.upper() == '#FE8463',
                            'site_progress_name': label}
    return result


def receipt_path(value):
    from runtime_paths import DATA_DIR
    if str(uuid.UUID(value)) != value:
        raise ValueError('Invalid lesson receipt id.')
    return DATA_DIR / 'lesson_receipts' / (value + '.json')


def save_lesson_receipt(username, lessons):
    value = str(uuid.uuid4())
    receipt = {'id':value, 'username':username, 'site':BASE,
        'fetched_at_utc':datetime.now(timezone.utc).isoformat(),
        'lessons':[{k:lesson[k] for k in ('command','topic','url')} |
                   {'sha256':hashlib.sha256(lesson['text'].encode('utf-8')).hexdigest(),
                    'characters':len(lesson['text'])} for lesson in lessons]}
    write_report(receipt_path(value), receipt)
    return receipt


def load_lesson_evidence(receipt_ids, username, topics, require_all=True):
    records = []
    covered = set()
    for value in receipt_ids:
        receipt = json.loads(receipt_path(value).read_text(encoding='utf-8'))
        if receipt.get('site') != BASE or receipt.get('username') != username:
            raise ValueError('Lesson receipt account/site mismatch.')
        for lesson in receipt['lessons']:
            name = lesson['command']
            if name in topics:
                expected = BASE + '/study/content/content_' + topics[name] + '.php'
                if lesson['topic'] != topics[name] or lesson['url'] != expected or not lesson['characters']:
                    raise ValueError('Lesson receipt topic/content mismatch.')
                covered.add(name)
        records.append(receipt)
    missing = set(topics) - covered
    if missing and require_all:
        raise ValueError('Fetch lessons before starting; missing: ' + ', '.join(sorted(missing)))
    return records
