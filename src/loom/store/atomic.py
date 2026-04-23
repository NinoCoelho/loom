"""Atomic file write — write-then-rename to avoid partial files.

Uses a temporary file in the same directory and :func:`os.replace` so the
rename is atomic on POSIX filesystems. The result is that readers never see
a partially-written file, even if the process is killed mid-write.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise
