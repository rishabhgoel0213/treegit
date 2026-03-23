from __future__ import annotations

from dataclasses import dataclass
import os
import sqlite3
import stat
from fnmatch import fnmatch
from os import PathLike
from pathlib import Path

from treegit.errors import UnsupportedFileError
from treegit.hashing import object_id
from treegit.models import WorkingFile


TEXT_SIZE_LIMIT = 1024 * 1024
SCAN_CACHE_VERSION = 2
SCAN_CACHE_NAME = "scan-cache.db"
SCAN_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    ctime_ns INTEGER NOT NULL,
    blob_id TEXT NOT NULL,
    is_text INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class ScanCacheEntry:
    kind: str
    size: int
    mtime_ns: int
    ctime_ns: int
    blob_id: str
    is_text: bool


class WorktreeScanCache:
    def __init__(self, git_dir: Path) -> None:
        self.path = git_dir / SCAN_CACHE_NAME

    def load(self) -> dict[str, ScanCacheEntry]:
        if not self.path.exists():
            return {}
        try:
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            return {}
        try:
            self._ensure_schema(conn)
            if not self._has_current_version(conn):
                self._reset(conn)
                return {}
            rows = conn.execute(
                """
                SELECT path, kind, size, mtime_ns, ctime_ns, blob_id, is_text
                FROM files
                ORDER BY path
                """
            ).fetchall()
            return {
                row["path"]: ScanCacheEntry(
                    kind=row["kind"],
                    size=row["size"],
                    mtime_ns=row["mtime_ns"],
                    ctime_ns=row["ctime_ns"],
                    blob_id=row["blob_id"],
                    is_text=bool(row["is_text"]),
                )
                for row in rows
            }
        except sqlite3.Error:
            return {}
        finally:
            conn.close()

    def save(self, changed_entries: dict[str, ScanCacheEntry], deleted_paths: set[str]) -> None:
        if not changed_entries and not deleted_paths:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        try:
            self._ensure_schema(conn)
            if not self._has_current_version(conn):
                self._reset(conn)
            conn.execute("BEGIN")
            if changed_entries:
                conn.executemany(
                    """
                    INSERT INTO files(path, kind, size, mtime_ns, ctime_ns, blob_id, is_text)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        kind = excluded.kind,
                        size = excluded.size,
                        mtime_ns = excluded.mtime_ns,
                        ctime_ns = excluded.ctime_ns,
                        blob_id = excluded.blob_id,
                        is_text = excluded.is_text
                    """,
                    [
                        (
                            path,
                            entry.kind,
                            entry.size,
                            entry.mtime_ns,
                            entry.ctime_ns,
                            entry.blob_id,
                            int(entry.is_text),
                        )
                        for path, entry in changed_entries.items()
                    ],
                )
            if deleted_paths:
                for chunk in _chunked(sorted(deleted_paths), 900):
                    placeholders = ",".join("?" for _ in chunk)
                    conn.execute(f"DELETE FROM files WHERE path IN ({placeholders})", tuple(chunk))
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(SCAN_CACHE_SCHEMA)
        conn.execute(
            """
            INSERT INTO meta(key, value)
            VALUES ('version', ?)
            ON CONFLICT(key) DO NOTHING
            """,
            (str(SCAN_CACHE_VERSION),),
        )
        conn.commit()

    def _has_current_version(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
        return row is not None and row[0] == str(SCAN_CACHE_VERSION)

    def _reset(self, conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM files")
        conn.execute(
            """
            INSERT INTO meta(key, value)
            VALUES ('version', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(SCAN_CACHE_VERSION),),
        )
        conn.commit()


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


def kind_for_stat(st_mode: int) -> str:
    if stat.S_ISREG(st_mode):
        return "file"
    if stat.S_ISLNK(st_mode):
        return "symlink"
    raise UnsupportedFileError("unsupported file type")


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


def read_working_file_raw(
    absolute: str | PathLike[str],
    relative: str,
    stat_result: os.stat_result | None = None,
) -> bytes:
    file_stat = stat_result or os.lstat(absolute)
    if stat.S_ISREG(file_stat.st_mode):
        with open(absolute, "rb") as handle:
            return handle.read()
    if stat.S_ISLNK(file_stat.st_mode):
        return os.readlink(absolute).encode("utf-8")
    raise UnsupportedFileError(f"unsupported special file: {relative}")


def _cache_entry_for_stat(stat_result: os.stat_result, blob_id: str, is_text: bool) -> ScanCacheEntry:
    return ScanCacheEntry(
        kind=kind_for_stat(stat_result.st_mode),
        size=stat_result.st_size,
        mtime_ns=stat_result.st_mtime_ns,
        ctime_ns=stat_result.st_ctime_ns,
        blob_id=blob_id,
        is_text=is_text,
    )


def _matches_cache(entry: ScanCacheEntry, stat_result: os.stat_result) -> bool:
    return (
        entry.kind == kind_for_stat(stat_result.st_mode)
        and entry.size == stat_result.st_size
        and entry.mtime_ns == stat_result.st_mtime_ns
        and entry.ctime_ns == stat_result.st_ctime_ns
    )


def scan_working_tree(repo_root: Path, git_dir: Path | None = None) -> dict[str, WorkingFile]:
    cache = None if git_dir is None else WorktreeScanCache(git_dir)
    cached_entries = {} if cache is None else cache.load()
    changed_entries: dict[str, ScanCacheEntry] = {}
    patterns = read_ignore_patterns(repo_root)
    snapshots: dict[str, WorkingFile] = {}
    seen_paths: set[str] = set()

    def walk(directory: str, prefix: str) -> None:
        with os.scandir(directory) as entries:
            for entry in entries:
                relative = f"{prefix}/{entry.name}" if prefix else entry.name
                if entry.name == ".treegit" or relative == ".treegit" or relative.startswith(".treegit/"):
                    continue
                if is_ignored(relative, patterns):
                    continue
                stat_result = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(stat_result.st_mode):
                    walk(entry.path, relative)
                    continue
                cached_entry = cached_entries.get(relative)
                mode = mode_for_stat(stat_result.st_mode)
                raw: bytes | None = None
                if cached_entry is None or not _matches_cache(cached_entry, stat_result):
                    raw = read_working_file_raw(entry.path, relative, stat_result)
                    is_text = len(raw) <= TEXT_SIZE_LIMIT and is_text_blob(raw)
                    blob_id = object_id("blob", raw)
                    changed_entries[relative] = _cache_entry_for_stat(stat_result, blob_id, is_text)
                else:
                    is_text = cached_entry.is_text
                    blob_id = cached_entry.blob_id
                snapshots[relative] = WorkingFile(
                    path=relative,
                    mode=mode,
                    raw=raw,
                    size=stat_result.st_size,
                    is_text=is_text,
                    blob_id=blob_id,
                )
                seen_paths.add(relative)

    walk(os.fspath(repo_root), "")
    deleted_paths = set(cached_entries) - seen_paths
    if cache is not None:
        cache.save(changed_entries, deleted_paths)
    return snapshots


def _chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]
