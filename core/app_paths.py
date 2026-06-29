"""Stable application paths for Abu Hassan Bot.

PyInstaller one-file builds run from a temporary _MEI... directory.  These
helpers keep config/data/log paths anchored to the real application folder when
running from source, a one-folder build, or RUN_SOURCE_FIXED.bat.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _candidate_root_from_executable() -> Path:
    try:
        exe = Path(sys.executable).resolve()
        if exe.name.lower().endswith('.exe'):
            # If running from dist/Abu Hassan Bot.exe, use parent of dist when possible.
            if exe.parent.name.lower() == 'dist':
                return exe.parent.parent
            return exe.parent
    except Exception:
        pass
    return Path.cwd().resolve()


def get_app_root() -> Path:
    env_root = os.environ.get('ABU_HASSAN_BOT_ROOT')
    if env_root:
        p = Path(env_root).expanduser().resolve()
        if p.exists():
            return p

    # Source tree: core/app_paths.py -> project root.
    source_root = Path(__file__).resolve().parents[1]
    if (source_root / 'main.py').exists() or (source_root / 'config').exists():
        return source_root

    return _candidate_root_from_executable()


def get_config_dir() -> Path:
    return get_app_root() / 'config'


def get_data_dir() -> Path:
    d = get_app_root() / 'data'
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_env_paths() -> tuple[Path, Path]:
    root = get_app_root()
    return root / '.env', root / 'config' / '.env'
