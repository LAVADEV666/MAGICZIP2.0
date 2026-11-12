"""Resource and writable locations for source and the Windows portable bundle."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path


FROZEN = bool(getattr(sys, 'frozen', False))
RESOURCE_ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1])).resolve()
# The frozen engine lives at <portable>/engine/MagicZipEngine.exe.
APP_ROOT = Path(sys.executable).resolve().parent.parent if FROZEN else RESOURCE_ROOT
WORK_ROOT = APP_ROOT / 'work'


def installation_id():
    normalized = str(APP_ROOT.resolve()).rstrip('\\/').lower()
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def picker_command(kind):
    if FROZEN:
        return [sys.executable, '--picker', kind]
    return [sys.executable, str(RESOURCE_ROOT / 'magiczip' / 'picker.py'), kind]
