from __future__ import annotations

import os
from pathlib import Path
from typing import TypeAlias


PathLike: TypeAlias = str | os.PathLike[str]


def path_identity_key(value: PathLike) -> str:
    """Return a stable, platform-native key for path comparisons and hashes."""

    path = Path(value).expanduser()
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        resolved = Path(os.path.abspath(os.fspath(path)))
    return os.path.normcase(os.path.normpath(os.fspath(resolved)))


def same_path(left: PathLike, right: PathLike) -> bool:
    """Compare path identity, including Windows short/long names when possible."""

    left_raw = os.fspath(left)
    right_raw = os.fspath(right)
    if not left_raw or not right_raw:
        return False
    try:
        if os.path.exists(left_raw) and os.path.exists(right_raw):
            return os.path.samefile(left_raw, right_raw)
    except (OSError, NotImplementedError, TypeError, ValueError):
        pass
    return path_identity_key(left_raw) == path_identity_key(right_raw)
