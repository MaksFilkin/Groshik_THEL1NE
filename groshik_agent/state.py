"""Registration state: agent_id, api_key, trusted_secret.

Written once by `python main.py register` to $THE_LINE_KEYDIR/registration.json (mode 600),
next to the wallet and signing keys the SDK keeps there. Never logged, never committed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

FILENAME = "registration.json"


def path(keydir: Path) -> Path:
    return keydir / FILENAME


def load(keydir: Path) -> Optional[dict]:
    p = path(keydir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def save(keydir: Path, data: dict) -> Path:
    keydir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(keydir, 0o700)
    except OSError:
        pass
    p = path(keydir)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, p)
    return p
