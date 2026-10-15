"""Use the installer's local environment when available."""
import os
from pathlib import Path
import sys
import subprocess

if __name__ == '__main__':
    root = Path(__file__).resolve().parent.parent
    interpreter = root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not interpreter.exists():
        interpreter = Path(sys.executable)
    # Windows execv can detach the replacement and lose its exit status.
    raise SystemExit(subprocess.run([str(interpreter), str(root / 'scripts/linuxstudio.py'),
                                     *sys.argv[1:]]).returncode)
