from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from treegit.errors import MCTSExecutionError, MCTSRunNotFoundError
from treegit.index import MetadataIndex
from treegit.mcts.models import EvalResult, SearchEvalRecord, SearchNodeRecord, SearchNoteRecord, SearchRunRecord
from treegit.mcts.spec import search_spec_from_json, search_spec_to_json


ACTIVE_SEARCH_ID = "__active__"

SCHEMA = """
CREATE TABLE IF NOT EXISTS mcts_runs (
    run_id TEXT PRIMARY KEY,
    root_branch TEXT NOT NULL,
    root_commit_id TEXT,
    config_json TEXT NOT NULL,
    next_child_index INTEGER NOT NULL,
    status TEXT NOT NULL,
    steps_completed INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mcts_nodes (
    run_id TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    parent_branch_name TEXT,
    commit_id TEXT,
    depth INTEGER NOT NULL,
    child_count INTEGER NOT NULL,
    visit_count INTEGER NOT NULL,
    value_sum REAL NOT NULL,
    last_utility REAL,
    last_raw_score REAL,
    last_eval_id TEXT,
    status TEXT NOT NULL,
    terminal_reason TEXT,
    worktree_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, branch_name)
);

CREATE TABLE IF NOT EXISTS mcts_evals (
    eval_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    commit_id TEXT NOT NULL,
    state_id TEXT NOT NULL,
    objective_id TEXT NOT NULL,
    objective_version TEXT NOT NULL,
    success INTEGER NOT NULL,
    raw_score REAL,
    utility REAL,
    direction TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    artifacts_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mcts_objective_cache (
    objective_id TEXT NOT NULL,
    objective_version TEXT NOT NULL,
    state_id TEXT NOT NULL,
    success INTEGER NOT NULL,
    raw_score REAL,
    utility REAL,
    direction TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    artifacts_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (objective_id, objective_version, state_id)
);

CREATE TABLE IF NOT EXISTS mcts_notes (
    run_id TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    parent_branch_name TEXT,
    note_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, branch_name)
);

CREATE INDEX IF NOT EXISTS mcts_nodes_run_status_idx
    ON mcts_nodes(run_id, status);
CREATE INDEX IF NOT EXISTS mcts_nodes_run_parent_idx
    ON mcts_nodes(run_id, parent_branch_name);
CREATE INDEX IF NOT EXISTS mcts_evals_run_branch_idx
    ON mcts_evals(run_id, branch_name);
CREATE INDEX IF NOT EXISTS mcts_notes_run_branch_idx
    ON mcts_notes(run_id, branch_name);
"""


class MCTSStore:
    def __init__(self, index: MetadataIndex) -> None:
        self.index = index

    def init(self) -> None:
        conn = self.index.connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def reset_search(self) -> None:
        conn = self.index.connect()
        try:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM mcts_evals")
            conn.execute("DELETE FROM mcts_notes")
            conn.execute("DELETE FROM mcts_nodes")
            conn.execute("DELETE FROM mcts_runs")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_search(
        self,
        root_branch: str,
        root_commit_id: str | None,
        spec,
        next_child_index: int,
    ) -> SearchRunRecord:
        now = _utcnow()
        config_json = search_spec_to_json(spec)
        conn = self.index.connect()
        try:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM mcts_evals")
            conn.execute("DELETE FROM mcts_notes")
            conn.execute("DELETE FROM mcts_nodes")
            conn.execute("DELETE FROM mcts_runs")
            conn.execute(
                """
                INSERT INTO mcts_runs(
                    run_id, root_branch, root_commit_id, config_json, next_child_index,
                    status, steps_completed, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'running', 0, ?, ?)
                """,
                (ACTIVE_SEARCH_ID, root_branch, root_commit_id, config_json, next_child_index, now, now),
            )
            conn.execute(
                """
                INSERT INTO mcts_nodes(
                    run_id, branch_name, parent_branch_name, commit_id, depth, child_count,
                    visit_count, value_sum, last_utility, last_raw_score, last_eval_id,
                    status, terminal_reason, worktree_path, created_at, updated_at
                )
                VALUES (?, ?, NULL, ?, 0, 0, 0, 0.0, NULL, NULL, NULL, 'ready', NULL, NULL, ?, ?)
                """,
                (ACTIVE_SEARCH_ID, root_branch, root_commit_id, now, now),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise MCTSExecutionError("failed to initialize MCTS search") from exc
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get_search()

    def get_search(self) -> SearchRunRecord:
        conn = self.index.connect()
        try:
            row = conn.execute(
                """
                SELECT root_branch, root_commit_id, config_json, next_child_index,
                       status, steps_completed, created_at, updated_at
                FROM mcts_runs
                WHERE run_id = ?
                """,
                (ACTIVE_SEARCH_ID,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise MCTSRunNotFoundError("MCTS search has not been initialized")
        return SearchRunRecord(
            root_branch=row["root_branch"],
            root_commit_id=row["root_commit_id"],
            spec=search_spec_from_json(row["config_json"]),
            next_child_index=int(row["next_child_index"]),
            status=row["status"],
            steps_completed=int(row["steps_completed"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def reserve_child_indices(self, count: int) -> list[int]:
        conn = self.index.connect()
        try:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT next_child_index FROM mcts_runs WHERE run_id = ?",
                (ACTIVE_SEARCH_ID,),
            ).fetchone()
            if row is None:
                raise MCTSRunNotFoundError("MCTS search has not been initialized")
            start = int(row["next_child_index"])
            conn.execute(
                """
                UPDATE mcts_runs
                SET next_child_index = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (start + count, _utcnow(), ACTIVE_SEARCH_ID),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return list(range(start, start + count))

    def list_frontier_nodes(self) -> list[SearchNodeRecord]:
        conn = self.index.connect()
        try:
            rows = conn.execute(
                """
                SELECT n.*, p.visit_count AS parent_visit_count
                FROM mcts_nodes n
                LEFT JOIN mcts_nodes p
                  ON p.run_id = n.run_id
                 AND p.branch_name = n.parent_branch_name
                WHERE n.run_id = ?
                  AND n.child_count = 0
                  AND n.status = 'ready'
                ORDER BY n.depth ASC, n.branch_name ASC
                """,
                (ACTIVE_SEARCH_ID,),
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_node(row) for row in rows]

    def get_node(self, branch_name: str) -> SearchNodeRecord | None:
        conn = self.index.connect()
        try:
            row = conn.execute(
                """
                SELECT n.*, p.visit_count AS parent_visit_count
                FROM mcts_nodes n
                LEFT JOIN mcts_nodes p
                  ON p.run_id = n.run_id
                 AND p.branch_name = n.parent_branch_name
                WHERE n.run_id = ?
                  AND n.branch_name = ?
                """,
                (ACTIVE_SEARCH_ID, branch_name),
            ).fetchone()
        finally:
            conn.close()
        return None if row is None else _row_to_node(row)

    def get_eval(self, eval_id: str) -> SearchEvalRecord | None:
        conn = self.index.connect()
        try:
            row = conn.execute(
                """
                SELECT eval_id, branch_name, commit_id, state_id, objective_id, objective_version,
                       success, raw_score, utility, direction, metrics_json, payload_json,
                       artifacts_json, created_at
                FROM mcts_evals
                WHERE eval_id = ?
                """,
                (eval_id,),
            ).fetchone()
        finally:
            conn.close()
        return None if row is None else _row_to_eval(row)

    def get_note(self, branch_name: str) -> SearchNoteRecord | None:
        conn = self.index.connect()
        try:
            row = conn.execute(
                """
                SELECT branch_name, parent_branch_name, note_text, created_at, updated_at
                FROM mcts_notes
                WHERE run_id = ? AND branch_name = ?
                """,
                (ACTIVE_SEARCH_ID, branch_name),
            ).fetchone()
        finally:
            conn.close()
        return None if row is None else _row_to_note(row)

    def upsert_note(self, branch_name: str, parent_branch_name: str | None, note_text: str) -> SearchNoteRecord:
        now = _utcnow()
        conn = self.index.connect()
        try:
            conn.execute(
                """
                INSERT INTO mcts_notes(run_id, branch_name, parent_branch_name, note_text, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, branch_name) DO UPDATE SET
                    parent_branch_name = excluded.parent_branch_name,
                    note_text = excluded.note_text,
                    updated_at = excluded.updated_at
                """,
                (ACTIVE_SEARCH_ID, branch_name, parent_branch_name, note_text, now, now),
            )
            conn.commit()
        finally:
            conn.close()
        note = self.get_note(branch_name)
        if note is None:
            raise MCTSExecutionError(f"failed to store note for {branch_name}")
        return note

    def lineage(self, branch_name: str) -> list[SearchNodeRecord]:
        conn = self.index.connect()
        try:
            current_branch = branch_name
            rows = []
            while current_branch is not None:
                row = conn.execute(
                    """
                    SELECT n.*, p.visit_count AS parent_visit_count
                    FROM mcts_nodes n
                    LEFT JOIN mcts_nodes p
                      ON p.run_id = n.run_id
                     AND p.branch_name = n.parent_branch_name
                    WHERE n.run_id = ? AND n.branch_name = ?
                    """,
                    (ACTIVE_SEARCH_ID, current_branch),
                ).fetchone()
                if row is None:
                    raise MCTSExecutionError(f"unknown MCTS node {current_branch!r}")
                rows.append(row)
                current_branch = row["parent_branch_name"]
        finally:
            conn.close()
        rows.reverse()
        return [_row_to_node(row) for row in rows]

    def mark_node_status(self, branch_name: str, status: str, terminal_reason: str | None = None) -> None:
        conn = self.index.connect()
        try:
            conn.execute(
                """
                UPDATE mcts_nodes
                SET status = ?, terminal_reason = ?, updated_at = ?
                WHERE run_id = ? AND branch_name = ?
                """,
                (status, terminal_reason, _utcnow(), ACTIVE_SEARCH_ID, branch_name),
            )
            conn.commit()
        finally:
            conn.close()

    def add_child_node(
        self,
        branch_name: str,
        parent_branch_name: str,
        depth: int,
        worktree_path: str,
    ) -> None:
        now = _utcnow()
        conn = self.index.connect()
        try:
            conn.execute("BEGIN")
            conn.execute(
                """
                INSERT INTO mcts_nodes(
                    run_id, branch_name, parent_branch_name, commit_id, depth, child_count,
                    visit_count, value_sum, last_utility, last_raw_score, last_eval_id,
                    status, terminal_reason, worktree_path, created_at, updated_at
                )
                VALUES (?, ?, ?, NULL, ?, 0, 0, 0.0, NULL, NULL, NULL, 'expanding', NULL, ?, ?, ?)
                """,
                (ACTIVE_SEARCH_ID, branch_name, parent_branch_name, depth, worktree_path, now, now),
            )
            conn.execute(
                """
                UPDATE mcts_nodes
                SET child_count = child_count + 1, updated_at = ?
                WHERE run_id = ? AND branch_name = ?
                """,
                (now, ACTIVE_SEARCH_ID, parent_branch_name),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_node_commit(self, branch_name: str, commit_id: str | None) -> None:
        conn = self.index.connect()
        try:
            conn.execute(
                """
                UPDATE mcts_nodes
                SET commit_id = ?, updated_at = ?
                WHERE run_id = ? AND branch_name = ?
                """,
                (commit_id, _utcnow(), ACTIVE_SEARCH_ID, branch_name),
            )
            conn.commit()
        finally:
            conn.close()

    def record_eval(
        self,
        eval_id: str,
        branch_name: str,
        commit_id: str,
        state_id: str,
        result: EvalResult,
        status: str,
        terminal_reason: str | None = None,
    ) -> SearchEvalRecord:
        now = _utcnow()
        metrics_json = json.dumps(result.metrics, sort_keys=True)
        payload_json = json.dumps(result.payload, sort_keys=True)
        artifacts_json = json.dumps(result.artifacts, sort_keys=True)
        conn = self.index.connect()
        try:
            conn.execute("BEGIN")
            conn.execute(
                """
                INSERT INTO mcts_evals(
                    eval_id, run_id, branch_name, commit_id, state_id, objective_id, objective_version,
                    success, raw_score, utility, direction, metrics_json, payload_json,
                    artifacts_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    eval_id,
                    ACTIVE_SEARCH_ID,
                    branch_name,
                    commit_id,
                    state_id,
                    result.objective_id,
                    result.objective_version,
                    int(result.success),
                    result.raw_score,
                    result.utility,
                    result.direction,
                    metrics_json,
                    payload_json,
                    artifacts_json,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO mcts_objective_cache(
                    objective_id, objective_version, state_id, success, raw_score, utility,
                    direction, metrics_json, payload_json, artifacts_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(objective_id, objective_version, state_id) DO UPDATE SET
                    success = excluded.success,
                    raw_score = excluded.raw_score,
                    utility = excluded.utility,
                    direction = excluded.direction,
                    metrics_json = excluded.metrics_json,
                    payload_json = excluded.payload_json,
                    artifacts_json = excluded.artifacts_json
                """,
                (
                    result.objective_id,
                    result.objective_version,
                    state_id,
                    int(result.success),
                    result.raw_score,
                    result.utility,
                    result.direction,
                    metrics_json,
                    payload_json,
                    artifacts_json,
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE mcts_nodes
                SET commit_id = ?,
                    last_eval_id = ?,
                    last_utility = ?,
                    last_raw_score = ?,
                    status = ?,
                    terminal_reason = ?,
                    updated_at = ?
                WHERE run_id = ? AND branch_name = ?
                """,
                (
                    commit_id,
                    eval_id,
                    result.utility,
                    result.raw_score,
                    status,
                    terminal_reason,
                    now,
                    ACTIVE_SEARCH_ID,
                    branch_name,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return SearchEvalRecord(
            eval_id=eval_id,
            branch_name=branch_name,
            commit_id=commit_id,
            state_id=state_id,
            result=result,
            created_at=now,
        )

    def get_cached_eval(self, objective_id: str, objective_version: str, state_id: str) -> EvalResult | None:
        conn = self.index.connect()
        try:
            row = conn.execute(
                """
                SELECT success, raw_score, utility, direction, metrics_json, payload_json, artifacts_json
                FROM mcts_objective_cache
                WHERE objective_id = ? AND objective_version = ? AND state_id = ?
                """,
                (objective_id, objective_version, state_id),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return EvalResult(
            success=bool(row["success"]),
            objective_id=objective_id,
            objective_version=objective_version,
            direction=row["direction"],
            raw_score=None if row["raw_score"] is None else float(row["raw_score"]),
            utility=None if row["utility"] is None else float(row["utility"]),
            metrics=json.loads(row["metrics_json"]),
            payload=json.loads(row["payload_json"]),
            artifacts=json.loads(row["artifacts_json"]),
        )

    def backprop(self, branch_name: str, utility: float) -> None:
        conn = self.index.connect()
        try:
            conn.execute("BEGIN")
            current_branch = branch_name
            now = _utcnow()
            while current_branch is not None:
                conn.execute(
                    """
                    UPDATE mcts_nodes
                    SET visit_count = visit_count + 1,
                        value_sum = value_sum + ?,
                        updated_at = ?
                    WHERE run_id = ? AND branch_name = ?
                    """,
                    (utility, now, ACTIVE_SEARCH_ID, current_branch),
                )
                row = conn.execute(
                    """
                    SELECT parent_branch_name
                    FROM mcts_nodes
                    WHERE run_id = ? AND branch_name = ?
                    """,
                    (ACTIVE_SEARCH_ID, current_branch),
                ).fetchone()
                current_branch = None if row is None else row["parent_branch_name"]
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def increment_steps(self, status: str | None = None) -> None:
        conn = self.index.connect()
        try:
            if status is None:
                conn.execute(
                    """
                    UPDATE mcts_runs
                    SET steps_completed = steps_completed + 1,
                        updated_at = ?
                    WHERE run_id = ?
                    """,
                    (_utcnow(), ACTIVE_SEARCH_ID),
                )
            else:
                conn.execute(
                    """
                    UPDATE mcts_runs
                    SET steps_completed = steps_completed + 1,
                        status = ?,
                        updated_at = ?
                    WHERE run_id = ?
                    """,
                    (status, _utcnow(), ACTIVE_SEARCH_ID),
                )
            conn.commit()
        finally:
            conn.close()

    def set_search_status(self, status: str) -> None:
        conn = self.index.connect()
        try:
            conn.execute(
                """
                UPDATE mcts_runs
                SET status = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (status, _utcnow(), ACTIVE_SEARCH_ID),
            )
            conn.commit()
        finally:
            conn.close()

    def frontier_count(self) -> int:
        conn = self.index.connect()
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM mcts_nodes
                WHERE run_id = ? AND child_count = 0 AND status = 'ready'
                """,
                (ACTIVE_SEARCH_ID,),
            ).fetchone()
        finally:
            conn.close()
        return int(row["count"])

    def best_node(self) -> SearchNodeRecord | None:
        conn = self.index.connect()
        try:
            row = conn.execute(
                """
                SELECT n.*, p.visit_count AS parent_visit_count
                FROM mcts_nodes n
                LEFT JOIN mcts_nodes p
                  ON p.run_id = n.run_id
                 AND p.branch_name = n.parent_branch_name
                WHERE n.run_id = ?
                  AND n.last_utility IS NOT NULL
                ORDER BY n.last_utility DESC, n.branch_name ASC
                LIMIT 1
                """,
                (ACTIVE_SEARCH_ID,),
            ).fetchone()
        finally:
            conn.close()
        return None if row is None else _row_to_node(row)

    def list_worktree_paths(self) -> list[str]:
        conn = self.index.connect()
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT worktree_path
                FROM mcts_nodes
                WHERE run_id = ?
                  AND worktree_path IS NOT NULL
                  AND worktree_path != ''
                ORDER BY worktree_path
                """,
                (ACTIVE_SEARCH_ID,),
            ).fetchall()
        finally:
            conn.close()
        return [str(row["worktree_path"]) for row in rows]

    def list_nodes(self) -> list[SearchNodeRecord]:
        conn = self.index.connect()
        try:
            rows = conn.execute(
                """
                SELECT n.*, p.visit_count AS parent_visit_count
                FROM mcts_nodes n
                LEFT JOIN mcts_nodes p
                  ON p.run_id = n.run_id
                 AND p.branch_name = n.parent_branch_name
                WHERE n.run_id = ?
                ORDER BY n.depth ASC, n.branch_name ASC
                """,
                (ACTIVE_SEARCH_ID,),
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_node(row) for row in rows]


def _row_to_node(row) -> SearchNodeRecord:
    return SearchNodeRecord(
        branch_name=row["branch_name"],
        parent_branch_name=row["parent_branch_name"],
        commit_id=row["commit_id"],
        depth=int(row["depth"]),
        child_count=int(row["child_count"]),
        visit_count=int(row["visit_count"]),
        value_sum=float(row["value_sum"]),
        last_utility=None if row["last_utility"] is None else float(row["last_utility"]),
        last_raw_score=None if row["last_raw_score"] is None else float(row["last_raw_score"]),
        last_eval_id=row["last_eval_id"],
        status=row["status"],
        terminal_reason=row["terminal_reason"],
        worktree_path=row["worktree_path"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        parent_visit_count=None if row["parent_visit_count"] is None else int(row["parent_visit_count"]),
    )


def _row_to_eval(row) -> SearchEvalRecord:
    objective_id = row["objective_id"]
    objective_version = row["objective_version"]
    return SearchEvalRecord(
        eval_id=row["eval_id"],
        branch_name=row["branch_name"],
        commit_id=row["commit_id"],
        state_id=row["state_id"],
        result=EvalResult(
            success=bool(row["success"]),
            objective_id=objective_id,
            objective_version=objective_version,
            direction=row["direction"],
            raw_score=None if row["raw_score"] is None else float(row["raw_score"]),
            utility=None if row["utility"] is None else float(row["utility"]),
            metrics=json.loads(row["metrics_json"]),
            payload=json.loads(row["payload_json"]),
            artifacts=json.loads(row["artifacts_json"]),
        ),
        created_at=row["created_at"],
    )


def _row_to_note(row) -> SearchNoteRecord:
    return SearchNoteRecord(
        branch_name=row["branch_name"],
        parent_branch_name=row["parent_branch_name"],
        note_text=row["note_text"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
