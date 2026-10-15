"""Build an allowlisted, data-free source/skill distribution."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent.parent
VERSION = '1.0.2'
SCRIPTS = ['launcher.py', 'linuxstudio.py', 'runtime_paths.py', 'linuxstudio_common.py',
           'linuxstudio_login.py', 'linuxstudio_session.py', 'practice_worker.py']
FILES = ['README.md', 'install.py', 'install.cmd', '.gitignore', '.gitattributes',
         '.github/workflows/tests.yml', 'tools/build.py',
         'tests/test_linuxstudio_login.py', 'tests/test_linuxstudio_session.py',
         'tests/test_practice.py', 'tests/test_distribution.py',
         'linuxstudio-practice/SKILL.md', 'linuxstudio-practice/agents/openai.yaml',
         'linuxstudio-practice/requirements.txt', 'linuxstudio-practice/run.cmd',
         'linuxstudio-practice/references/cli.md'] + ['linuxstudio-practice/scripts/' + s for s in SCRIPTS]


def build(output):
    hashes = {}
    for name in sorted(FILES):
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            raise RuntimeError('Required regular source file missing: ' + name)
        content = path.read_bytes()
        content.decode('utf-8')  # Skill metadata and every source must be portable UTF-8.
        # Hard-coded user locations must not enter a portable release.
        if b'C:\\Users\\' in content or b'D:\\Anaconda' in content:
            raise RuntimeError('Machine-specific path in ' + name)
        hashes[name] = hashlib.sha256(content).hexdigest()
    manifest = {'name': 'linuxstudio-practice', 'version': VERSION, 'files': hashes}
    (ROOT / 'MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'linuxstudio-practice-v{VERSION}.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as package:
        for name in [*sorted(FILES), 'MANIFEST.json']:
            package.write(ROOT / name, f'linuxstudio-practice-v{VERSION}/' + name)
    return {'archive': str(archive), 'files': len(FILES) + 1,
            'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT.parent / 'dist')
    args = parser.parse_args()
    print(json.dumps(build(args.output), indent=2))
