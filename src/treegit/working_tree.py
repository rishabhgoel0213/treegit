from __future__ import annotations

import os
import stat
from fnmatch import fnmatch
from pathlib import Path

from treegit.errors import UnsupportedFileError
from treegit.hashing import object_id
from treegit.models import WorkingFile


TEXT_SIZE_LIMIT = 1024 * 1024


def read_ignore_patterns(repo_root: Path) -> list[str]:
    ignore_file = repo_root / ".treegitignore"
    if not ignore_file.exists():
        return []
    patterns: list[str] = []
    for line in ignore_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        patterns.append(stripped)
    return patterns


def is_ignored(relative_path: str, patterns: list[str]) -> bool:
    return any(fnmatch(relative_path, pattern) for pattern in patterns)


def mode_for_stat(st_mode: int) -> str:
    if stat.S_ISLNK(st_mode):
        return "120000"
    if st_mode & stat.S_IXUSR:
        return "100755"
    return "100644"


def is_text_blob(raw: bytes) -> bool:
    if not raw:
        return True
    if b"\x00" in raw:
        return False
    sample = raw[:8192]
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def scan_working_tree(repo_root: Path) -> dict[str, WorkingFile]:
    patterns = read_ignore_patterns(repo_root)
    snapshots: dict[str, WorkingFile] = {}
    for root, dirnames, filenames in os.walk(repo_root, topdown=True):
        root_path = Path(root)
        rel_root = root_path.relative_to(repo_root)
        prefix = "" if rel_root == Path(".") else rel_root.as_posix()
        dirnames[:] = [
            name
            for name in dirnames
            if name != ".treegit"
            and not is_ignored(f"{prefix}/{name}" if prefix else name, patterns)
        ]
        for name in filenames:
            absolute = root_path / name
            relative = f"{prefix}/{name}" if prefix else name
            if relative.startswith(".treegit/") or relative == ".treegit":
                continue
            if is_ignored(relative, patterns):
                continue
            stat_result = os.lstat(absolute)
            if stat.S_ISREG(stat_result.st_mode):
                raw = absolute.read_bytes()
            elif stat.S_ISLNK(stat_result.st_mode):
                raw = os.readlink(absolute).encode("utf-8")
            else:
                raise UnsupportedFileError(f"unsupported special file: {relative}")
            mode = mode_for_stat(stat_result.st_mode)
            is_text = len(raw) <= TEXT_SIZE_LIMIT and is_text_blob(raw)
            snapshots[relative] = WorkingFile(
                path=relative,
                mode=mode,
                raw=raw,
                size=len(raw),
                is_text=is_text,
                blob_id=object_id("blob", raw),
            )
    return dict(sorted(snapshots.items()))
