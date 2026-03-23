from __future__ import annotations

from fnmatch import fnmatch
import sqlite3
from pathlib import Path

from treegit.models import BranchRecord, CommitRecord, FileSnapshot


SQLITE_BATCH_SIZE = 900


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS refs (
    name TEXT PRIMARY KEY,
    commit_id TEXT,
    parent_name TEXT,
    fork_commit_id TEXT
);

CREATE TABLE IF NOT EXISTS head (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    kind TEXT NOT NULL,
    target TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commits (
    commit_id TEXT PRIMARY KEY,
    parent_id TEXT,
    root_tree_id TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commit_files (
    commit_id TEXT NOT NULL,
    path TEXT NOT NULL,
    blob_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    size INTEGER NOT NULL,
    is_text INTEGER NOT NULL,
    PRIMARY KEY (commit_id, path)
);

CREATE TABLE IF NOT EXISTS commit_changes (
    commit_id TEXT NOT NULL,
    path TEXT NOT NULL,
    blob_id TEXT,
    mode TEXT,
    size INTEGER,
    is_text INTEGER,
    deleted INTEGER NOT NULL,
    PRIMARY KEY (commit_id, path)
);

CREATE TABLE IF NOT EXISTS commit_manifest_state (
    commit_id TEXT PRIMARY KEY,
    ready INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS branch_tip_files (
    branch_name TEXT NOT NULL,
    path TEXT NOT NULL,
    blob_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    size INTEGER NOT NULL,
    is_text INTEGER NOT NULL,
    PRIMARY KEY (branch_name, path)
);

CREATE TABLE IF NOT EXISTS blobs (
    blob_id TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    is_text INTEGER NOT NULL,
    indexed_content INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS commit_files_commit_idx ON commit_files(commit_id);
CREATE INDEX IF NOT EXISTS commit_files_path_idx ON commit_files(path);
CREATE INDEX IF NOT EXISTS commit_changes_commit_idx ON commit_changes(commit_id);
CREATE INDEX IF NOT EXISTS commits_parent_idx ON commits(parent_id);
CREATE INDEX IF NOT EXISTS branch_tip_files_branch_idx ON branch_tip_files(branch_name);

CREATE VIRTUAL TABLE IF NOT EXISTS blob_fts USING fts5(
    blob_id UNINDEXED,
    content
);

CREATE VIRTUAL TABLE IF NOT EXISTS commit_fts USING fts5(
    commit_id UNINDEXED,
    message
);

CREATE VIRTUAL TABLE IF NOT EXISTS branch_fts USING fts5(
    name
);

CREATE TABLE IF NOT EXISTS metrics (
    name TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS branch_metrics (
    branch_name TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (branch_name, metric_name),
    FOREIGN KEY (branch_name) REFERENCES refs(name),
    FOREIGN KEY (metric_name) REFERENCES metrics(name)
);

CREATE INDEX IF NOT EXISTS branch_metrics_metric_idx ON branch_metrics(metric_name);
"""


class MetadataIndex:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
            conn.execute(
                """
                INSERT INTO head(id, kind, target)
                VALUES (1, 'branch', 'root')
                ON CONFLICT(id) DO NOTHING
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO refs(name, commit_id, parent_name, fork_commit_id)
                VALUES ('root', NULL, NULL, NULL)
                """
            )
            conn.execute("INSERT OR IGNORE INTO branch_fts(name) VALUES ('root')")
            conn.execute(
                """
                INSERT OR IGNORE INTO commit_manifest_state(commit_id, ready)
                SELECT c.commit_id,
                       CASE
                           WHEN c.parent_id IS NULL THEN 1
                           WHEN EXISTS(
                               SELECT 1
                               FROM commit_files cf
                               WHERE cf.commit_id = c.commit_id
                           ) THEN 1
                           ELSE 0
                       END
                FROM commits c
                """
            )
            branches = conn.execute(
                """
                SELECT name, commit_id
                FROM refs
                WHERE commit_id IS NOT NULL
                ORDER BY name
                """
            ).fetchall()
            for branch in branches:
                exists = conn.execute(
                    "SELECT 1 FROM branch_tip_files WHERE branch_name = ? LIMIT 1",
                    (branch["name"],),
                ).fetchone()
                if exists is not None:
                    continue
                conn.execute(
                    """
                    INSERT INTO branch_tip_files(branch_name, path, blob_id, mode, size, is_text)
                    SELECT ?, path, blob_id, mode, size, is_text
                    FROM commit_files
                    WHERE commit_id = ?
                    """,
                    (branch["name"], branch["commit_id"]),
                )
            conn.commit()
        finally:
            conn.close()

    def read_head(self) -> tuple[str, str]:
        conn = self.connect()
        try:
            row = conn.execute("SELECT kind, target FROM head WHERE id = 1").fetchone()
            return row["kind"], row["target"]
        finally:
            conn.close()

    def update_head(self, kind: str, target: str) -> None:
        conn = self.connect()
        try:
            conn.execute("UPDATE head SET kind = ?, target = ? WHERE id = 1", (kind, target))
            conn.commit()
        finally:
            conn.close()

    def list_branches(self) -> list[BranchRecord]:
        conn = self.connect()
        try:
            rows = conn.execute(
                """
                SELECT name, commit_id, parent_name, fork_commit_id
                FROM refs
                ORDER BY name
                """
            ).fetchall()
            return [
                BranchRecord(
                    name=row["name"],
                    commit_id=row["commit_id"],
                    parent_name=row["parent_name"],
                    fork_commit_id=row["fork_commit_id"],
                )
                for row in rows
            ]
        finally:
            conn.close()

    def get_branch(self, name: str) -> BranchRecord | None:
        conn = self.connect()
        try:
            row = conn.execute(
                """
                SELECT name, commit_id, parent_name, fork_commit_id
                FROM refs
                WHERE name = ?
                """,
                (name,),
            ).fetchone()
            if row is None:
                return None
            return BranchRecord(
                name=row["name"],
                commit_id=row["commit_id"],
                parent_name=row["parent_name"],
                fork_commit_id=row["fork_commit_id"],
            )
        finally:
            conn.close()

    def get_ref(self, name: str) -> str | None:
        conn = self.connect()
        try:
            row = conn.execute("SELECT commit_id FROM refs WHERE name = ?", (name,)).fetchone()
            return None if row is None else row["commit_id"]
        finally:
            conn.close()

    def has_ref(self, name: str) -> bool:
        conn = self.connect()
        try:
            row = conn.execute("SELECT 1 FROM refs WHERE name = ?", (name,)).fetchone()
            return row is not None
        finally:
            conn.close()

    def create_branch(
        self,
        name: str,
        commit_id: str | None,
        parent_name: str | None,
        fork_commit_id: str | None,
    ) -> None:
        conn = self.connect()
        try:
            conn.execute(
                """
                INSERT INTO refs(name, commit_id, parent_name, fork_commit_id)
                VALUES (?, ?, ?, ?)
                """,
                (name, commit_id, parent_name, fork_commit_id),
            )
            conn.execute("INSERT INTO branch_fts(name) VALUES (?)", (name,))
            metrics = conn.execute("SELECT name FROM metrics ORDER BY name").fetchall()
            for metric in metrics:
                conn.execute(
                    """
                    INSERT INTO branch_metrics(branch_name, metric_name, value)
                    VALUES (?, ?, 0.0)
                    """,
                    (name, metric["name"]),
                )
            conn.commit()
        finally:
            conn.close()

    def set_ref(self, name: str, commit_id: str) -> None:
        conn = self.connect()
        try:
            conn.execute("UPDATE refs SET commit_id = ? WHERE name = ?", (commit_id, name))
            conn.commit()
        finally:
            conn.close()

    def commit_exists(self, commit_id: str) -> bool:
        conn = self.connect()
        try:
            row = conn.execute("SELECT 1 FROM commits WHERE commit_id = ?", (commit_id,)).fetchone()
            return row is not None
        finally:
            conn.close()

    def get_commit(self, commit_id: str) -> CommitRecord | None:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT commit_id, parent_id, root_tree_id, message, created_at FROM commits WHERE commit_id = ?",
                (commit_id,),
            ).fetchone()
            if row is None:
                return None
            return CommitRecord(
                commit_id=row["commit_id"],
                parent_id=row["parent_id"],
                root_tree_id=row["root_tree_id"],
                message=row["message"],
                created_at=row["created_at"],
            )
        finally:
            conn.close()

    def unindexed_text_blob_ids(self, blob_ids: set[str]) -> set[str]:
        if not blob_ids:
            return set()
        conn = self.connect()
        try:
            indexed: set[str] = set()
            for chunk in _chunked(sorted(blob_ids), SQLITE_BATCH_SIZE):
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT blob_id FROM blob_fts WHERE blob_id IN ({placeholders})",
                    tuple(chunk),
                ).fetchall()
                indexed.update(row["blob_id"] for row in rows)
            return blob_ids - indexed
        finally:
            conn.close()

    def write_commit(
        self,
        commit: CommitRecord,
        files: list[FileSnapshot],
        branch_name: str | None,
        blob_contents: dict[str, str],
        parent_commit_id: str | None = None,
        deleted_paths: set[str] | None = None,
    ) -> None:
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            conn.execute(
                """
                INSERT INTO commits(commit_id, parent_id, root_tree_id, message, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (commit.commit_id, commit.parent_id, commit.root_tree_id, commit.message, commit.created_at),
            )
            conn.execute("INSERT INTO commit_fts(commit_id, message) VALUES (?, ?)", (commit.commit_id, commit.message))
            file_rows = [
                (commit.commit_id, item.path, item.blob_id, item.mode, item.size, int(item.is_text))
                for item in files
            ]
            conn.execute(
                """
                INSERT INTO commit_manifest_state(commit_id, ready)
                VALUES (?, ?)
                ON CONFLICT(commit_id) DO UPDATE SET ready = excluded.ready
                """,
                (commit.commit_id, int(parent_commit_id is None)),
            )
            if file_rows:
                conn.executemany(
                    """
                    INSERT INTO commit_changes(commit_id, path, blob_id, mode, size, is_text, deleted)
                    VALUES (?, ?, ?, ?, ?, ?, 0)
                    """,
                    file_rows,
                )
            if deleted_paths:
                conn.executemany(
                    """
                    INSERT INTO commit_changes(commit_id, path, blob_id, mode, size, is_text, deleted)
                    VALUES (?, ?, NULL, NULL, NULL, NULL, 1)
                    """,
                    [(commit.commit_id, path) for path in sorted(deleted_paths)],
                )
            if file_rows:
                if parent_commit_id is None:
                    conn.executemany(
                        """
                        INSERT INTO commit_files(commit_id, path, blob_id, mode, size, is_text)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        file_rows,
                    )
                blob_rows: dict[str, tuple[str, int, int, int]] = {}
                for item in files:
                    blob_rows[item.blob_id] = (
                        item.blob_id,
                        item.size,
                        int(item.is_text),
                        int(item.blob_id in blob_contents),
                    )
                conn.executemany(
                    """
                    INSERT INTO blobs(blob_id, size, is_text, indexed_content)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(blob_id) DO UPDATE SET
                        indexed_content = CASE
                            WHEN blobs.indexed_content = 1 OR excluded.indexed_content = 1 THEN 1
                            ELSE 0
                        END
                    """,
                    list(blob_rows.values()),
                )
            if blob_contents:
                existing: set[str] = set()
                for chunk in _chunked(sorted(blob_contents), SQLITE_BATCH_SIZE):
                    placeholders = ",".join("?" for _ in chunk)
                    rows = conn.execute(
                        f"SELECT blob_id FROM blob_fts WHERE blob_id IN ({placeholders})",
                        tuple(chunk),
                    ).fetchall()
                    existing.update(row["blob_id"] for row in rows)
                pending = [
                    (blob_id, content)
                    for blob_id, content in blob_contents.items()
                    if blob_id not in existing
                ]
                if pending:
                    conn.executemany(
                        "INSERT INTO blob_fts(blob_id, content) VALUES (?, ?)",
                        pending,
                    )
            if branch_name is not None:
                self._apply_branch_tip_changes(conn, branch_name, files, deleted_paths or set(), parent_commit_id is None)
                conn.execute("UPDATE refs SET commit_id = ? WHERE name = ?", (commit.commit_id, branch_name))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _apply_branch_tip_changes(
        self,
        conn: sqlite3.Connection,
        branch_name: str,
        files: list[FileSnapshot],
        deleted_paths: set[str],
        replace_all: bool,
    ) -> None:
        if replace_all:
            conn.execute("DELETE FROM branch_tip_files WHERE branch_name = ?", (branch_name,))
        elif deleted_paths:
            for chunk in _chunked(sorted(deleted_paths), SQLITE_BATCH_SIZE):
                placeholders = ",".join("?" for _ in chunk)
                conn.execute(
                    f"DELETE FROM branch_tip_files WHERE branch_name = ? AND path IN ({placeholders})",
                    (branch_name, *chunk),
                )
        if not files:
            return
        conn.executemany(
            """
            INSERT INTO branch_tip_files(branch_name, path, blob_id, mode, size, is_text)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(branch_name, path) DO UPDATE SET
                blob_id = excluded.blob_id,
                mode = excluded.mode,
                size = excluded.size,
                is_text = excluded.is_text
            """,
            [
                (branch_name, item.path, item.blob_id, item.mode, item.size, int(item.is_text))
                for item in files
            ],
        )

    def branch_tip_files(self, branch_name: str) -> list[FileSnapshot]:
        conn = self.connect()
        try:
            rows = conn.execute(
                """
                SELECT path, mode, blob_id, size, is_text
                FROM branch_tip_files
                WHERE branch_name = ?
                ORDER BY path
                """,
                (branch_name,),
            ).fetchall()
            return _rows_to_snapshots(rows)
        finally:
            conn.close()

    def replace_branch_tip(self, branch_name: str, files: list[FileSnapshot]) -> None:
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM branch_tip_files WHERE branch_name = ?", (branch_name,))
            self._apply_branch_tip_changes(conn, branch_name, files, set(), replace_all=False)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def commit_manifest_ready(self, commit_id: str) -> bool:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT ready FROM commit_manifest_state WHERE commit_id = ?",
                (commit_id,),
            ).fetchone()
            return row is not None and bool(row["ready"])
        finally:
            conn.close()

    def commit_changes(self, commit_id: str) -> tuple[list[FileSnapshot], set[str]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                """
                SELECT path, mode, blob_id, size, is_text, deleted
                FROM commit_changes
                WHERE commit_id = ?
                ORDER BY path
                """,
                (commit_id,),
            ).fetchall()
            files: list[FileSnapshot] = []
            deleted_paths: set[str] = set()
            for row in rows:
                if row["deleted"]:
                    deleted_paths.add(row["path"])
                    continue
                files.append(
                    FileSnapshot(
                        path=row["path"],
                        mode=row["mode"],
                        blob_id=row["blob_id"],
                        size=row["size"],
                        is_text=bool(row["is_text"]),
                    )
                )
            return files, deleted_paths
        finally:
            conn.close()

    def store_commit_manifest(self, commit_id: str, files: list[FileSnapshot]) -> None:
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM commit_files WHERE commit_id = ?", (commit_id,))
            if files:
                conn.executemany(
                    """
                    INSERT INTO commit_files(commit_id, path, blob_id, mode, size, is_text)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (commit_id, item.path, item.blob_id, item.mode, item.size, int(item.is_text))
                        for item in files
                    ],
                )
            conn.execute(
                """
                INSERT INTO commit_manifest_state(commit_id, ready)
                VALUES (?, 1)
                ON CONFLICT(commit_id) DO UPDATE SET ready = 1
                """,
                (commit_id,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def files_for_commit(self, commit_id: str) -> list[FileSnapshot]:
        conn = self.connect()
        try:
            rows = conn.execute(
                """
                SELECT path, mode, blob_id, size, is_text
                FROM commit_files
                WHERE commit_id = ?
                ORDER BY path
                """,
                (commit_id,),
            ).fetchall()
            return _rows_to_snapshots(rows)
        finally:
            conn.close()

    def reachable_commits(self, branch_names: list[str] | None = None) -> dict[str, set[str]]:
        conn = self.connect()
        try:
            if branch_names:
                placeholders = ",".join("?" for _ in branch_names)
                rows = conn.execute(
                    f"""
                    WITH RECURSIVE reachable(name, commit_id) AS (
                        SELECT name, commit_id
                        FROM refs
                        WHERE name IN ({placeholders}) AND commit_id IS NOT NULL
                        UNION
                        SELECT reachable.name, commits.parent_id
                        FROM reachable
                        JOIN commits ON commits.commit_id = reachable.commit_id
                        WHERE commits.parent_id IS NOT NULL
                    )
                    SELECT name, commit_id
                    FROM reachable
                    ORDER BY name, commit_id
                    """,
                    tuple(branch_names),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    WITH RECURSIVE reachable(name, commit_id) AS (
                        SELECT name, commit_id
                        FROM refs
                        WHERE commit_id IS NOT NULL
                        UNION
                        SELECT reachable.name, commits.parent_id
                        FROM reachable
                        JOIN commits ON commits.commit_id = reachable.commit_id
                        WHERE commits.parent_id IS NOT NULL
                    )
                    SELECT name, commit_id
                    FROM reachable
                    ORDER BY name, commit_id
                    """
                ).fetchall()
            commits: dict[str, set[str]] = {}
            for row in rows:
                commits.setdefault(row["name"], set()).add(row["commit_id"])
            return commits
        finally:
            conn.close()

    def search_branches(self, query: str, limit: int) -> list[str]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT name FROM branch_fts WHERE branch_fts MATCH ? ORDER BY rank LIMIT ?",
                (query, limit),
            ).fetchall()
            return [row["name"] for row in rows]
        finally:
            conn.close()

    def has_metric(self, name: str) -> bool:
        conn = self.connect()
        try:
            row = conn.execute("SELECT 1 FROM metrics WHERE name = ?", (name,)).fetchone()
            return row is not None
        finally:
            conn.close()

    def define_metric(self, name: str, default: float = 0.0) -> None:
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            conn.execute("INSERT INTO metrics(name) VALUES (?)", (name,))
            branches = conn.execute("SELECT name FROM refs ORDER BY name").fetchall()
            for branch in branches:
                conn.execute(
                    """
                    INSERT INTO branch_metrics(branch_name, metric_name, value)
                    VALUES (?, ?, ?)
                    """,
                    (branch["name"], name, default),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_branch_metric(self, branch_name: str, metric_name: str) -> float | None:
        conn = self.connect()
        try:
            exists = conn.execute("SELECT 1 FROM metrics WHERE name = ?", (metric_name,)).fetchone()
            if exists is None:
                return None
            row = conn.execute(
                """
                SELECT value
                FROM branch_metrics
                WHERE branch_name = ? AND metric_name = ?
                """,
                (branch_name, metric_name),
            ).fetchone()
            if row is None:
                return 0.0
            return float(row["value"])
        finally:
            conn.close()

    def increment_metric_for_branches(self, metric_name: str, branch_names: list[str], delta: float) -> None:
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            for branch_name in branch_names:
                conn.execute(
                    """
                    INSERT INTO branch_metrics(branch_name, metric_name, value)
                    VALUES (?, ?, 0.0)
                    ON CONFLICT(branch_name, metric_name) DO NOTHING
                    """,
                    (branch_name, metric_name),
                )
                conn.execute(
                    """
                    UPDATE branch_metrics
                    SET value = value + ?
                    WHERE branch_name = ? AND metric_name = ?
                    """,
                    (delta, branch_name, metric_name),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def search_commits(self, query: str, reachable: set[str], limit: int) -> list[CommitRecord]:
        if not reachable:
            return []
        conn = self.connect()
        try:
            placeholders = ",".join("?" for _ in reachable)
            rows = conn.execute(
                f"""
                SELECT c.commit_id, c.parent_id, c.root_tree_id, c.message, c.created_at
                FROM commit_fts f
                JOIN commits c ON c.commit_id = f.commit_id
                WHERE f.message MATCH ?
                  AND c.commit_id IN ({placeholders})
                ORDER BY c.created_at DESC
                LIMIT ?
                """,
                (query, *reachable, limit),
            ).fetchall()
            return [
                CommitRecord(
                    commit_id=row["commit_id"],
                    parent_id=row["parent_id"],
                    root_tree_id=row["root_tree_id"],
                    message=row["message"],
                    created_at=row["created_at"],
                )
                for row in rows
            ]
        finally:
            conn.close()

    def search_content(
        self,
        query: str,
        reachable: set[str],
        path_glob: str | None,
        limit: int,
    ) -> list[sqlite3.Row]:
        if not reachable:
            return []
        conn = self.connect()
        try:
            placeholders = ",".join("?" for _ in reachable)
            rows = conn.execute(
                f"""
                SELECT cf.commit_id, cf.path, c.created_at, b.content
                FROM blob_fts b
                JOIN commit_files cf ON cf.blob_id = b.blob_id
                JOIN commits c ON c.commit_id = cf.commit_id
                WHERE b.content MATCH ?
                  AND cf.commit_id IN ({placeholders})
                ORDER BY c.created_at DESC
                """,
                (query, *reachable),
            ).fetchall()
            if path_glob is None:
                return rows[:limit]
            filtered = [row for row in rows if fnmatch(row["path"], path_glob)]
            return filtered[:limit]
        finally:
            conn.close()

    def search_paths(
        self,
        query: str,
        reachable: set[str],
        path_glob: str | None,
        limit: int,
    ) -> list[sqlite3.Row]:
        if not reachable:
            return []
        conn = self.connect()
        try:
            placeholders = ",".join("?" for _ in reachable)
            rows = conn.execute(
                f"""
                SELECT DISTINCT cf.commit_id, cf.path, c.created_at
                FROM commit_files cf
                JOIN commits c ON c.commit_id = cf.commit_id
                WHERE lower(cf.path) LIKE ?
                  AND cf.commit_id IN ({placeholders})
                ORDER BY c.created_at DESC
                """,
                (f"%{query.lower()}%", *reachable),
            ).fetchall()
            if path_glob is None:
                return rows[:limit]
            filtered = [row for row in rows if fnmatch(row["path"], path_glob)]
            return filtered[:limit]
        finally:
            conn.close()


def _chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _rows_to_snapshots(rows: list[sqlite3.Row]) -> list[FileSnapshot]:
    return [
        FileSnapshot(
            path=row["path"],
            mode=row["mode"],
            blob_id=row["blob_id"],
            size=row["size"],
            is_text=bool(row["is_text"]),
        )
        for row in rows
    ]
