"""Private runtime files are independent of the installed skill directory."""
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get('LINUXSTUDIO_DATA_DIR') or
                str(Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) /
                    'LinuxStudioPractice')).expanduser().resolve()
PRIVATE_DIR = DATA_DIR / 'private'
JOBS_DIR = DATA_DIR / 'jobs'
