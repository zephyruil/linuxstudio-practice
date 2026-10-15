"""Install the bundled skill for the current user, without user data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent


def verify_package(root=ROOT):
    manifest = json.loads((root / 'MANIFEST.json').read_text(encoding='utf-8'))
    for name, digest in manifest['files'].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file() or path.is_symlink():
            raise RuntimeError('Invalid package member: ' + name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError('Package integrity mismatch: ' + name)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skills-dir', type=Path, default=Path.home() / '.agents' / 'skills')
    parser.add_argument('--no-deps', action='store_true', help='Use caller Python; skip dependency installation.')
    parser.add_argument('--upgrade', action='store_true', help='Preserve existing installation as a dated backup.')
    args = parser.parse_args(argv)
    if sys.version_info < (3, 10):
        raise RuntimeError('Python 3.10 or newer is required.')
    if os.name != 'nt':
        raise RuntimeError('Version 1 supports Windows only.')
    verify_package()
    skills_dir = args.skills_dir.expanduser().resolve()
    target = skills_dir / 'linuxstudio-practice'
    if target.is_symlink() or target.resolve().parent != skills_dir:
        raise RuntimeError('Unsafe installation target.')
    if target.exists() and not args.upgrade:
        raise RuntimeError('Skill already exists. Use --upgrade to preserve it as a backup.')
    skills_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.linuxstudio-install-', dir=skills_dir) as temporary:
        staged = Path(temporary) / 'linuxstudio-practice'
        # Copy only manifest-listed skill source files, never runtime extras.
        for name in verify_package()['files']:
            member = Path(name)
            if member.parts[0] != 'linuxstudio-practice':
                continue
            destination = staged.joinpath(*member.parts[1:])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / member, destination)
        backup = None
        if target.exists():
            backup = skills_dir / ('.linuxstudio-practice-backup-' + str(time.time_ns()))
            if backup.resolve().parent != skills_dir:
                raise RuntimeError('Unsafe backup target.')
            target.rename(backup)
        try:
            staged.rename(target)
            if not args.no_deps:
                subprocess.run([sys.executable, '-m', 'venv', str(target / '.venv')], check=True)
                python = target / '.venv' / 'Scripts' / 'python.exe'
                subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(target / 'requirements.txt')], check=True)
        except Exception:
            # Retain a partial installation for inspection and restore old code.
            if target.exists():
                target.rename(skills_dir / ('.linuxstudio-practice-failed-' + str(time.time_ns())))
            if backup:
                backup.rename(target)
            raise
    print(json.dumps({'status': 'installed', 'skill': str(target),
                      'backup': str(backup) if backup else None,
                      'entry': str(target / 'run.cmd')}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
