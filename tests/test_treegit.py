from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from treegit.errors import (  # noqa: E402
    BranchNavigationError,
    CheckoutConflictError,
    DirtyWorkingTreeError,
    InvalidObjectError,
    MetricExistsError,
    MetricNotFoundError,
    UnsupportedFileError,
)
from treegit.cli import build_parser, run_command  # noqa: E402
from treegit.mcts import MCTSEngine  # noqa: E402
from treegit.hashing import object_id  # noqa: E402
from treegit.models import TreeEntry  # noqa: E402
from treegit.objects import parse_tree, serialize_commit, serialize_tree  # noqa: E402
from treegit.repository import Repository  # noqa: E402


class TreeGitTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def init_repo(self) -> Repository:
        return self.init_repo_at(self.workspace)

    def init_repo_at(self, path: Path) -> Repository:
        path.mkdir(parents=True, exist_ok=True)
        return Repository.init(path)

    def write_text(self, relative_path: str, content: str, executable: bool = False) -> None:
        target = self.workspace / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if executable:
            target.chmod(0o755)

    def write_bytes(self, relative_path: str, content: bytes) -> None:
        target = self.workspace / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def make_symlink(self, relative_path: str, target: str) -> None:
        link = self.workspace / relative_path
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.exists() or link.is_symlink():
            link.unlink()
        os.symlink(target, link)

    def run_cli(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC_ROOT)
        return subprocess.run(
            [sys.executable, "-m", "treegit", *args],
            cwd=cwd or self.workspace,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def assert_output_path_matches(self, stdout: str, expected_path: Path) -> None:
        reported_path = None
        for line in stdout.splitlines():
            if line.startswith("output: "):
                reported_path = Path(line.split(": ", 1)[1])
                break
        self.assertIsNotNone(reported_path)
        assert reported_path is not None
        self.assertEqual(reported_path.resolve(), expected_path.resolve())

    def read_scan_cache_blob_id(self, cache_path: Path, relative_path: str) -> str | None:
        conn = sqlite3.connect(cache_path)
        try:
            row = conn.execute("SELECT blob_id FROM files WHERE path = ?", (relative_path,)).fetchone()
            return None if row is None else row[0]
        finally:
            conn.close()


class ObjectModelTests(TreeGitTestCase):
    def test_canonical_hashing_for_tree_and_commit_objects(self) -> None:
        entries_one = [
            TreeEntry(name="z.txt", mode="100644", kind="blob", object_id="b" * 64),
            TreeEntry(name="a.txt", mode="100755", kind="blob", object_id="a" * 64),
        ]
        entries_two = list(reversed(entries_one))

        payload_one = serialize_tree(entries_one)
        payload_two = serialize_tree(entries_two)

        self.assertEqual(payload_one, payload_two)
        self.assertEqual(object_id("tree", payload_one), object_id("tree", payload_two))

        commit_one = serialize_commit(None, "c" * 64, "message", "2026-03-16T00:00:00Z")
        commit_two = serialize_commit(None, "c" * 64, "message", "2026-03-16T00:00:00Z")

        self.assertEqual(commit_one, commit_two)
        self.assertEqual(object_id("commit", commit_one), object_id("commit", commit_two))

    def test_changed_content_rewrites_only_affected_subtree(self) -> None:
        repo = self.init_repo()
        self.write_text("root.txt", "v1\n")
        self.write_text("dir/nested.txt", "stable\n")

        first_commit = repo.commit("first")
        first_meta = repo.index.get_commit(first_commit)
        self.assertIsNotNone(first_meta)
        first_root_kind, first_root_payload = repo.store.read_object(first_meta.root_tree_id)
        self.assertEqual(first_root_kind, "tree")
        first_root_entries = parse_tree(first_root_payload)
        first_dir_tree_id = next(entry.object_id for entry in first_root_entries if entry.name == "dir")
        nested_first = repo._file_map_for_commit(first_commit)["dir/nested.txt"].blob_id

        self.write_text("root.txt", "v2\n")
        second_commit = repo.commit("second")
        second_meta = repo.index.get_commit(second_commit)
        self.assertIsNotNone(second_meta)
        second_root_kind, second_root_payload = repo.store.read_object(second_meta.root_tree_id)
        self.assertEqual(second_root_kind, "tree")
        second_root_entries = parse_tree(second_root_payload)
        second_dir_tree_id = next(entry.object_id for entry in second_root_entries if entry.name == "dir")
        nested_second = repo._file_map_for_commit(second_commit)["dir/nested.txt"].blob_id

        self.assertNotEqual(first_meta.root_tree_id, second_meta.root_tree_id)
        self.assertEqual(first_dir_tree_id, second_dir_tree_id)
        self.assertEqual(nested_first, nested_second)


class RepositoryIntegrationTests(TreeGitTestCase):
    def test_commit_status_diff_log_and_checkout_round_trip(self) -> None:
        repo = self.init_repo()
        self.assertEqual(repo.current_branch(), "root")
        self.write_text("script.sh", "echo one\n")
        self.make_symlink("current", "script.sh")

        first_commit = repo.commit("initial snapshot")

        self.write_text("script-v2.sh", "echo two\n")
        self.write_text("script.sh", "echo one updated\n", executable=True)
        self.make_symlink("current", "script-v2.sh")

        status = repo.status()
        self.assertEqual(status.added, ["script-v2.sh"])
        self.assertEqual(sorted(status.modified), ["current", "script.sh"])
        diff_output = repo.diff()
        self.assertIn("Mode changed: script.sh 100644 -> 100755", diff_output)
        self.assertIn("+echo one updated", diff_output)

        repo.create_branch("restore-first")
        second_commit = repo.commit("second snapshot")
        log_messages = [entry.message for entry in repo.log()]
        self.assertEqual(log_messages, ["second snapshot", "initial snapshot"])

        repo.checkout("restore-first", force=True)
        self.assertEqual((self.workspace / "script.sh").read_text(encoding="utf-8"), "echo one\n")
        self.assertFalse(bool((self.workspace / "script.sh").stat().st_mode & stat.S_IXUSR))
        self.assertEqual(os.readlink(self.workspace / "current"), "script.sh")

        repo.checkout("root", force=True)
        self.assertTrue(bool((self.workspace / "script.sh").stat().st_mode & stat.S_IXUSR))
        self.assertEqual(os.readlink(self.workspace / "current"), "script-v2.sh")
        self.assertEqual(second_commit, repo.resolve_revision("root"))

    def test_branch_tree_navigation_and_conflicts(self) -> None:
        repo = self.init_repo()
        self.assertEqual(repo.current_branch(), "root")
        self.write_text("main.txt", "root\n")
        root_commit = repo.commit("root base")
        repo.create_branch("feature")
        repo.create_branch("alt")

        repo.checkout("feature", force=True)
        self.write_text("main.txt", "feature\n")
        self.write_text("dir/nested.txt", "branch only\n")
        repo.commit("feature work")
        repo.create_branch("leaf")

        repo.checkout("root", force=True)
        self.assertEqual((self.workspace / "main.txt").read_text(encoding="utf-8"), "root\n")

        self.write_text("main.txt", "dirty\n")
        with self.assertRaises(DirtyWorkingTreeError):
            repo.checkout("feature")

        repo.checkout("feature", force=True)
        self.assertEqual((self.workspace / "main.txt").read_text(encoding="utf-8"), "feature\n")

        repo.checkout("root", force=True)
        repo.checkout("feature", force=True)
        with self.assertRaises(BranchNavigationError):
            repo.checkout("alt", force=True)

        repo.checkout("leaf", force=True)
        self.assertEqual(repo.current_branch(), "leaf")
        repo.checkout("root", force=True)
        self.assertEqual(repo.current_branch(), "root")
        self.assertEqual((self.workspace / "main.txt").read_text(encoding="utf-8"), "root\n")
        repo.checkout("feature", force=True)
        self.assertEqual(repo.current_branch(), "feature")

        repo.checkout("root", force=True)
        self.write_text("dir", "untracked conflict\n")
        with self.assertRaises(CheckoutConflictError):
            repo.checkout("feature", force=True)

        self.assertEqual(root_commit, repo.resolve_revision("root"))

    def test_search_across_branches_paths_and_content_indexing(self) -> None:
        repo = self.init_repo()
        self.assertEqual(repo.current_branch(), "root")
        self.write_text("docs/readme.txt", "alpha token\n")
        self.write_text("shared.txt", "shared payload\n")
        repo.commit("alpha commit")
        repo.create_branch("feature")

        repo.checkout("feature", force=True)
        content = "beta token\nshared payload\n"
        self.write_text("src/app.py", content)
        self.write_text("src/dup.py", content)
        self.write_bytes("assets/binary.bin", b"\x00binary token\x00")
        self.write_text("assets/large.txt", "hugeword " * 150000)
        feature_commit = repo.commit("beta feature commit")

        branch_results = repo.search("feature", field="branch")
        self.assertEqual([item.ref for item in branch_results["branch"]], ["feature"])

        commit_results = repo.search("beta", field="commit")
        self.assertEqual([item.ref for item in commit_results["commit"]], [feature_commit])

        content_results = repo.search("beta", field="content", branch="feature")
        content_paths = [item.path for item in content_results["content"]]
        self.assertEqual(content_paths, ["src/app.py", "src/dup.py"])

        scoped_root = repo.search("beta", field="content", branch="root")
        self.assertEqual(scoped_root, {})

        path_results = repo.search("app", field="path", path_glob="src/*")
        self.assertEqual([item.path for item in path_results["path"]], ["src/app.py"])

        self.assertEqual(repo.search("hugeword", field="content"), {})
        self.assertEqual(repo.search("binary", field="content"), {})

        feature_files = repo._file_map_for_commit(feature_commit)
        duplicate_blob_id = feature_files["src/app.py"].blob_id
        self.assertEqual(feature_files["src/dup.py"].blob_id, duplicate_blob_id)

        conn = repo.index.connect()
        try:
            count = conn.execute("SELECT COUNT(*) AS count FROM blob_fts WHERE blob_id = ?", (duplicate_blob_id,)).fetchone()
            self.assertEqual(count["count"], 1)
        finally:
            conn.close()

    def test_failure_cases_for_special_files_missing_objects_and_interrupted_commit(self) -> None:
        repo = self.init_repo()
        fifo_path = self.workspace / "named-pipe"
        os.mkfifo(fifo_path)
        with self.assertRaises(UnsupportedFileError):
            repo.status()
        fifo_path.unlink()

        self.assertEqual(repo.current_branch(), "root")
        self.write_text("tracked.txt", "safe\n")
        first_commit = repo.commit("first")
        repo.create_branch("backup")
        blob_id = repo._file_map_for_commit(first_commit)["tracked.txt"].blob_id
        repo.store.path_for(blob_id).unlink()
        self.write_text("tracked.txt", "changed before checkout\n")
        with self.assertRaises(InvalidObjectError):
            repo.checkout("backup", force=True)

        self.write_text("tracked.txt", "updated\n")
        stable_head = repo.head_commit_id()
        with mock.patch.object(repo.index, "write_commit", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                repo.commit("interrupted")
        self.assertEqual(repo.head_commit_id(), stable_head)

    def test_no_op_commit_reuses_cached_blobs_without_rereading_files(self) -> None:
        repo = self.init_repo()
        self.write_text("tracked.txt", "stable\n")
        repo.commit("first")

        with mock.patch("treegit.working_tree.read_working_file_raw", side_effect=AssertionError("scan reread")), \
            mock.patch("treegit.repository.read_working_file_raw", side_effect=AssertionError("commit reread")):
            repo.commit("second")

    def test_one_file_change_only_rereads_that_path(self) -> None:
        repo = self.init_repo()
        self.write_text("changed.txt", "v1\n")
        self.write_text("stable.txt", "stable\n")
        repo.commit("first")

        self.write_text("changed.txt", "v2\n")

        original_reader = sys.modules["treegit.working_tree"].read_working_file_raw
        reread_paths: list[str] = []

        def record_reread(path: Path, relative: str, stat_result: os.stat_result | None = None) -> bytes:
            reread_paths.append(relative)
            return original_reader(path, relative, stat_result)

        with mock.patch("treegit.working_tree.read_working_file_raw", side_effect=record_reread), \
            mock.patch("treegit.repository.read_working_file_raw", side_effect=AssertionError("unexpected commit reread")):
            repo.commit("second")

        self.assertEqual(reread_paths, ["changed.txt"])

    def test_diff_reads_cached_added_file_contents_when_raw_bytes_are_elided(self) -> None:
        repo = self.init_repo()
        self.write_text("draft.txt", "draft\n")

        repo.status()
        cached_files = repo._scan_working_tree()
        self.assertIsNone(cached_files["draft.txt"].raw)

        diff_output = repo.diff()
        self.assertIn("+draft", diff_output)

    def test_noop_diff_avoids_blob_reads_for_identical_files(self) -> None:
        repo = self.init_repo()
        self.write_text("tracked.txt", "stable\n")
        repo.commit("first")
        repo.status()

        with mock.patch("treegit.working_tree.read_working_file_raw", side_effect=AssertionError("scan reread")), \
            mock.patch("treegit.repository.read_working_file_raw", side_effect=AssertionError("blob reread")), \
            mock.patch.object(repo.store, "read_object", side_effect=AssertionError("store read")):
            self.assertEqual(repo.diff(), "")

    def test_sparse_checkout_only_reads_changed_blobs(self) -> None:
        repo = self.init_repo()
        self.write_text("changed.txt", "root\n")
        self.write_text("stable.txt", "shared\n")
        repo.commit("root base")
        repo.create_branch("feature")

        repo.checkout("feature", force=True)
        self.write_text("changed.txt", "feature\n")
        repo.commit("feature work")

        repo.checkout("root", force=True)

        original_reader = repo.store.read_object
        read_blob_ids: list[str] = []

        def record_read(blob_id: str) -> tuple[str, bytes]:
            read_blob_ids.append(blob_id)
            return original_reader(blob_id)

        with mock.patch.object(repo.store, "read_object", side_effect=record_read):
            repo.checkout("feature", force=True)

        self.assertEqual(len(read_blob_ids), 1)

    def test_commit_manifests_materialize_lazily_for_history_queries(self) -> None:
        repo = self.init_repo()
        self.write_text("tracked.txt", "v1\n")
        first_commit = repo.commit("first")

        self.write_text("tracked.txt", "v2\n")
        second_commit = repo.commit("second")

        conn = repo.index.connect()
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS count FROM commit_files WHERE commit_id = ?",
                (second_commit,),
            ).fetchone()
            self.assertEqual(count["count"], 0)
        finally:
            conn.close()

        diff_output = repo.diff(first_commit, second_commit)
        self.assertIn("+v2", diff_output)

        conn = repo.index.connect()
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS count FROM commit_files WHERE commit_id = ?",
                (second_commit,),
            ).fetchone()
            self.assertGreater(count["count"], 0)
        finally:
            conn.close()

    def test_branch_tip_files_track_branch_heads(self) -> None:
        repo = self.init_repo()
        self.write_text("tracked.txt", "root\n")
        root_commit = repo.commit("root base")
        root_blob_id = repo._head_file_map()["tracked.txt"].blob_id

        repo.create_branch("feature")

        conn = repo.index.connect()
        try:
            root_row = conn.execute(
                "SELECT blob_id FROM branch_tip_files WHERE branch_name = ? AND path = ?",
                ("root", "tracked.txt"),
            ).fetchone()
            feature_row = conn.execute(
                "SELECT blob_id FROM branch_tip_files WHERE branch_name = ? AND path = ?",
                ("feature", "tracked.txt"),
            ).fetchone()
            self.assertEqual(root_row["blob_id"], root_blob_id)
            self.assertEqual(feature_row["blob_id"], root_blob_id)
        finally:
            conn.close()

        repo.checkout("feature", force=True)
        self.write_text("tracked.txt", "feature\n")
        feature_commit = repo.commit("feature work")

        conn = repo.index.connect()
        try:
            root_row = conn.execute(
                "SELECT blob_id FROM branch_tip_files WHERE branch_name = ? AND path = ?",
                ("root", "tracked.txt"),
            ).fetchone()
            feature_row = conn.execute(
                "SELECT blob_id FROM branch_tip_files WHERE branch_name = ? AND path = ?",
                ("feature", "tracked.txt"),
            ).fetchone()
            self.assertEqual(repo.resolve_revision("root"), root_commit)
            self.assertEqual(repo.resolve_revision("feature"), feature_commit)
            self.assertEqual(root_row["blob_id"], root_blob_id)
            self.assertNotEqual(feature_row["blob_id"], root_blob_id)
        finally:
            conn.close()

    def test_linked_worktrees_keep_branch_state_local_while_sharing_history(self) -> None:
        main_dir = self.workspace / "main"
        feature_dir = self.workspace / "feature"
        alt_dir = self.workspace / "alt"

        repo = self.init_repo_at(main_dir)
        (main_dir / "shared.txt").write_text("root\n", encoding="utf-8")
        root_commit = repo.commit("root base")
        repo.create_branch("feature")
        repo.create_branch("alt")

        feature_repo = repo.add_worktree(feature_dir, "feature")
        alt_repo = repo.add_worktree(alt_dir, "alt")

        self.assertEqual(feature_repo.common_dir, repo.common_dir)
        self.assertEqual(alt_repo.common_dir, repo.common_dir)
        self.assertEqual(repo.current_branch(), "root")
        self.assertEqual(feature_repo.current_branch(), "feature")
        self.assertEqual(alt_repo.current_branch(), "alt")
        self.assertEqual((feature_dir / "shared.txt").read_text(encoding="utf-8"), "root\n")
        self.assertEqual((alt_dir / "shared.txt").read_text(encoding="utf-8"), "root\n")

        (feature_dir / "shared.txt").write_text("feature\n", encoding="utf-8")
        feature_commit = feature_repo.commit("feature work")

        self.assertEqual(repo.resolve_revision("root"), root_commit)
        self.assertEqual(repo.resolve_revision("feature"), feature_commit)
        self.assertEqual(repo.current_branch(), "root")
        self.assertEqual(alt_repo.current_branch(), "alt")
        self.assertEqual((main_dir / "shared.txt").read_text(encoding="utf-8"), "root\n")
        self.assertEqual((alt_dir / "shared.txt").read_text(encoding="utf-8"), "root\n")
        self.assertFalse(repo.status().is_dirty())
        self.assertFalse(alt_repo.status().is_dirty())

        repo.checkout("feature", force=True)
        self.assertEqual(repo.current_branch(), "feature")
        self.assertEqual(alt_repo.current_branch(), "alt")
        self.assertEqual((main_dir / "shared.txt").read_text(encoding="utf-8"), "feature\n")
        self.assertEqual((alt_dir / "shared.txt").read_text(encoding="utf-8"), "root\n")

    def test_linked_worktrees_keep_separate_scan_caches(self) -> None:
        main_dir = self.workspace / "main"
        feature_dir = self.workspace / "feature"

        repo = self.init_repo_at(main_dir)
        (main_dir / "shared.txt").write_text("root\n", encoding="utf-8")
        repo.commit("root base")
        repo.create_branch("feature")
        feature_repo = repo.add_worktree(feature_dir, "feature")

        repo.status()
        feature_repo.status()

        main_cache_path = main_dir / ".treegit" / "scan-cache.db"
        feature_cache_path = feature_dir / ".treegit" / "scan-cache.db"
        self.assertTrue(main_cache_path.exists())
        self.assertTrue(feature_cache_path.exists())

        (feature_dir / "shared.txt").write_text("feature\n", encoding="utf-8")
        feature_repo.commit("feature work")

        self.assertNotEqual(
            self.read_scan_cache_blob_id(main_cache_path, "shared.txt"),
            self.read_scan_cache_blob_id(feature_cache_path, "shared.txt"),
        )
        self.assertEqual((main_dir / "shared.txt").read_text(encoding="utf-8"), "root\n")

    def test_metrics_define_get_backprop_and_auto_initialize_new_branches(self) -> None:
        repo = self.init_repo()
        self.write_text("main.txt", "root\n")
        repo.commit("root base")
        repo.create_branch("feature")
        repo.checkout("feature", force=True)
        repo.create_branch("leaf")
        repo.checkout("root", force=True)
        repo.create_branch("alt")

        repo.define_metric("score")
        self.assertEqual(repo.get_metric("score"), 0.0)

        repo.checkout("feature", force=True)
        self.assertEqual(repo.get_metric("score"), 0.0)
        repo.checkout("leaf", force=True)
        self.assertEqual(repo.get_metric("score"), 0.0)

        repo.backprop_metric("score", 1.5)
        self.assertEqual(repo.get_metric("score"), 1.5)

        repo.checkout("feature", force=True)
        self.assertEqual(repo.get_metric("score"), 1.5)
        repo.create_branch("newleaf")

        repo.checkout("root", force=True)
        self.assertEqual(repo.get_metric("score"), 1.5)

        repo.checkout("alt", force=True)
        self.assertEqual(repo.get_metric("score"), 0.0)

        repo.checkout("root", force=True)
        repo.checkout("feature", force=True)
        repo.checkout("newleaf", force=True)
        self.assertEqual(repo.get_metric("score"), 0.0)

    def test_metric_errors_for_duplicate_and_unknown_names(self) -> None:
        repo = self.init_repo()
        repo.define_metric("score")

        with self.assertRaises(MetricExistsError):
            repo.define_metric("score")

        with self.assertRaises(MetricNotFoundError):
            repo.get_metric("missing")

        with self.assertRaises(MetricNotFoundError):
            repo.backprop_metric("missing", 1.0)

    def test_worktree_add_can_rebind_existing_worktree_to_another_branch(self) -> None:
        main_dir = self.workspace / "main"
        feature_dir = self.workspace / "feature"

        repo = self.init_repo_at(main_dir)
        (main_dir / "shared.txt").write_text("root\n", encoding="utf-8")
        repo.commit("root base")
        repo.create_branch("feature")
        repo.create_branch("alt")

        feature_repo = repo.add_worktree(feature_dir, "feature")
        (feature_dir / "shared.txt").write_text("feature\n", encoding="utf-8")
        feature_commit = feature_repo.commit("feature work")

        rebound_repo = repo.add_worktree(feature_dir, "alt")
        self.assertEqual(rebound_repo.current_branch(), "alt")
        self.assertEqual((feature_dir / "shared.txt").read_text(encoding="utf-8"), "root\n")
        self.assertEqual(repo.resolve_revision("feature"), feature_commit)


class CliSmokeTests(TreeGitTestCase):
    def build_mcts_fixture(
        self,
        repo_root: Path,
        *,
        expander_env: dict[str, str] | None = None,
        selection_policy: str = "ucb_budgeted",
    ) -> tuple[Path, Path]:
        scripts_dir = self.workspace / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        counter_path = self.workspace / "objective-counter.txt"
        expander_path = scripts_dir / "fixture_expander.py"
        objective_path = scripts_dir / "fixture_objective.py"
        expander_path.write_text(
            """#!/usr/bin/env python3
from pathlib import Path
import os
import time

worktree = Path(os.environ["TREEGIT_WORKTREE"])
branch = os.environ["TREEGIT_BRANCH"]
parent_branch = os.environ["TREEGIT_PARENT_BRANCH"]
agent_name = os.environ["TREEGIT_AGENT_NAME"]
agent_slot = int(os.environ["TREEGIT_AGENT_SLOT"])
context_dir = worktree / ".treegit" / "mcts"
current_change = context_dir / "current_change.md"
template = current_change.read_text(encoding="utf-8")
if f"Branch: {branch}" not in template or f"Parent: {parent_branch}" not in template:
    raise SystemExit("missing current change template context")
barrier_dir_raw = os.environ.get("BARRIER_DIR")
if barrier_dir_raw:
    barrier_dir = Path(barrier_dir_raw)
    barrier_dir.mkdir(parents=True, exist_ok=True)
    expected = int(os.environ["BARRIER_EXPECTED"])
    token = barrier_dir / f"agent{agent_slot}.started"
    token.write_text(branch + "\\n", encoding="utf-8")
    deadline = time.time() + 5.0
    while time.time() < deadline:
        started = list(barrier_dir.glob("*.started"))
        if len(started) >= expected:
            break
        time.sleep(0.05)
    else:
        raise SystemExit("expander barrier timed out")
if parent_branch != "root":
    change_history = (context_dir / "change_history.md").read_text(encoding="utf-8")
    score_history = (context_dir / "score_history.md").read_text(encoding="utf-8")
    if "Branch: mcts/000002" not in change_history:
        raise SystemExit("missing parent change history")
    if "- utility: 2" not in score_history:
        raise SystemExit("missing parent score history")
current_change.write_text(
    f\"\"\"# Current Branch Change Note

Fill in this file before you stop. The search harness will aggregate it into the lineage history for descendants.

Branch: {branch}
Parent: {parent_branch}
Agent: {agent_name}

Summary:
Set fixture utility marker for {branch}.
Hypothesis:
Higher slot utility should win UCT tie-breaks in the fixture objective.
Files Changed:
- utility.txt
Validation:
- not run
Notes:
- fixture note
\"\"\",
    encoding="utf-8",
)
(worktree / "utility.txt").write_text(f"{agent_slot}\\n", encoding="utf-8")
""",
            encoding="utf-8",
        )
        expander_path.chmod(0o755)
        objective_path.write_text(
            """#!/usr/bin/env python3
from pathlib import Path
import json
import os

counter_path = Path(os.environ["COUNTER_PATH"])
count = 0
if counter_path.exists():
    count = int(counter_path.read_text(encoding="utf-8").strip())
counter_path.write_text(f"{count + 1}\\n", encoding="utf-8")
worktree = Path(os.environ["TREEGIT_WORKTREE"])
utility = float((worktree / "utility.txt").read_text(encoding="utf-8").strip())
print(json.dumps({
    "success": True,
    "direction": "maximize",
    "raw_score": utility,
    "utility": utility,
    "metrics": {"utility": utility},
    "artifacts": {"utility_path": str(worktree / "utility.txt")},
}))
""",
            encoding="utf-8",
        )
        objective_path.chmod(0o755)
        config_path = self.workspace / "mcts-fixture.json"
        config_path.write_text(
            json.dumps(
                {
                    "root_branch": "root",
                    "worktree_root": str((self.workspace / "worktrees").resolve()),
                    "branch_prefix": "mcts",
                    "iteration_budget": 2,
                    "selection": {
                        "policy": selection_policy,
                        "exploration_constant": 0.0,
                        "widening_coefficient": 2.0,
                        "widening_exponent": 0.5,
                        "virtual_loss": 1.0,
                    },
                    "expander": {
                        "command": [sys.executable, str(expander_path)],
                        "env": {} if expander_env is None else expander_env,
                        "commit_message_template": "expand {parent_branch} -> {branch}",
                    },
                    "objective": {
                        "id": "fixture-objective",
                        "version": "v1",
                        "command": [sys.executable, str(objective_path)],
                        "env": {"COUNTER_PATH": str(counter_path)},
                        "default_direction": "maximize",
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return config_path, counter_path

    def test_mcts_expanders_run_in_parallel_before_objective_queue(self) -> None:
        main_dir = self.workspace / "main"
        repo = self.init_repo_at(main_dir)
        (main_dir / "seed.txt").write_text("root\n", encoding="utf-8")
        repo.commit("root base")
        barrier_dir = self.workspace / "barrier"
        config_path, counter_path = self.build_mcts_fixture(
            main_dir,
            expander_env={
                "BARRIER_DIR": str(barrier_dir),
                "BARRIER_EXPECTED": "2",
            },
        )

        engine = MCTSEngine(repo)
        engine.init_search(config_path)
        first = engine.step()

        self.assertEqual([child.status for child in first.children], ["ready", "ready"])
        started = sorted(path.name for path in barrier_dir.glob("*.started"))
        self.assertEqual(started, ["agent1.started", "agent2.started"])
        self.assertEqual(counter_path.read_text(encoding="utf-8").strip(), "2")

    def test_mcts_engine_two_steps_selects_best_leaf(self) -> None:
        main_dir = self.workspace / "main"
        repo = self.init_repo_at(main_dir)
        (main_dir / "seed.txt").write_text("root\n", encoding="utf-8")
        repo.commit("root base")
        config_path, _ = self.build_mcts_fixture(main_dir)

        engine = MCTSEngine(repo)
        engine.init_search(config_path)

        first = engine.step()
        self.assertEqual(first.selected_parents, ["root", "root"])
        self.assertEqual([child.branch_name for child in first.children], [
            "mcts/000001",
            "mcts/000002",
        ])
        self.assertEqual([child.utility for child in first.children], [1.0, 2.0])
        self.assertEqual(
            [Path(child.worktree_path).name for child in first.children],
            ["agent1", "agent2"],
        )

        root_node = engine.store.get_node("root")
        self.assertIsNotNone(root_node)
        assert root_node is not None
        self.assertEqual(root_node.child_count, 2)
        self.assertEqual(root_node.visit_count, 2)
        self.assertEqual(root_node.value_sum, 3.0)
        self.assertEqual(root_node.status, "ready")

        best_after_first = engine.best()
        self.assertIsNotNone(best_after_first)
        assert best_after_first is not None
        self.assertEqual(best_after_first.branch_name, "mcts/000002")
        self.assertEqual(best_after_first.last_utility, 2.0)
        first_note = engine.store.get_note("mcts/000002")
        self.assertIsNotNone(first_note)
        assert first_note is not None
        self.assertIn("Set fixture utility marker for mcts/000002.", first_note.note_text)

        second = engine.step()
        self.assertEqual(second.selected_parents, ["mcts/000002", "root"])
        self.assertEqual([child.branch_name for child in second.children], [
            "mcts/000003",
            "mcts/000004",
        ])
        self.assertEqual([child.utility for child in second.children], [1.0, 2.0])
        self.assertEqual([child.status for child in second.children], ["ready", "ready"])
        self.assertEqual([child.parent_branch_name for child in second.children], ["mcts/000002", "root"])

        selected_node = engine.store.get_node("mcts/000002")
        self.assertIsNotNone(selected_node)
        assert selected_node is not None
        self.assertEqual(selected_node.child_count, 1)
        self.assertEqual(selected_node.status, "ready")

        grandchild = engine.store.get_node("mcts/000004")
        self.assertIsNotNone(grandchild)
        assert grandchild is not None
        self.assertEqual(grandchild.parent_branch_name, "root")
        self.assertEqual(grandchild.last_utility, 2.0)
        self.assertEqual(grandchild.status, "ready")
        self.assertIsNone(grandchild.terminal_reason)
        second_note = engine.store.get_note("mcts/000003")
        self.assertIsNotNone(second_note)
        assert second_note is not None
        self.assertIn("Set fixture utility marker for mcts/000003.", second_note.note_text)

        status = engine.status()
        self.assertEqual(status["steps_completed"], 2)
        self.assertEqual(status["frontier_count"], 5)
        self.assertEqual(status["best_branch"], "mcts/000002")

    def test_mcts_engine_uct_policy_expands_one_selected_frontier(self) -> None:
        main_dir = self.workspace / "main"
        repo = self.init_repo_at(main_dir)
        (main_dir / "seed.txt").write_text("root\n", encoding="utf-8")
        repo.commit("root base")
        config_path, _ = self.build_mcts_fixture(main_dir, selection_policy="uct")

        engine = MCTSEngine(repo)
        engine.init_search(config_path)

        first = engine.step()
        self.assertEqual(first.selected_parents, ["root", "root"])
        self.assertEqual([child.parent_branch_name for child in first.children], ["root", "root"])

        second = engine.step()
        self.assertEqual(second.selected_parents, ["mcts/000002", "mcts/000002"])
        self.assertEqual([child.parent_branch_name for child in second.children], ["mcts/000002", "mcts/000002"])
        self.assertEqual([child.branch_name for child in second.children], ["mcts/000003", "mcts/000004"])
        self.assertEqual([child.utility for child in second.children], [1.0, None])
        self.assertEqual([child.status for child in second.children], ["ready", "terminal"])

        selected_node = engine.store.get_node("mcts/000002")
        self.assertIsNotNone(selected_node)
        assert selected_node is not None
        self.assertEqual(selected_node.child_count, 2)

        status = engine.status()
        self.assertEqual(status["frontier_count"], 4)

    def test_mcts_objective_cache_reuses_equivalent_states_across_search_resets(self) -> None:
        main_dir = self.workspace / "main"
        repo = self.init_repo_at(main_dir)
        (main_dir / "seed.txt").write_text("root\n", encoding="utf-8")
        repo.commit("root base")
        config_path, counter_path = self.build_mcts_fixture(main_dir)

        engine = MCTSEngine(repo)
        engine.init_search(config_path)
        engine.step()
        self.assertEqual(counter_path.read_text(encoding="utf-8").strip(), "2")

        engine.init_search(config_path)
        engine.step()
        self.assertEqual(counter_path.read_text(encoding="utf-8").strip(), "2")

    def test_cli_mcts_round_trip(self) -> None:
        main_dir = self.workspace / "main"
        main_dir.mkdir()

        init = self.run_cli("init", cwd=main_dir)
        self.assertEqual(init.returncode, 0, init.stderr)

        (main_dir / "seed.txt").write_text("root\n", encoding="utf-8")
        commit_root = self.run_cli("commit", "-m", "root base", cwd=main_dir)
        self.assertEqual(commit_root.returncode, 0, commit_root.stderr)

        config_path, _ = self.build_mcts_fixture(main_dir)

        init_run = self.run_cli("mcts", "init", "--config", str(config_path), cwd=main_dir)
        self.assertEqual(init_run.returncode, 0, init_run.stderr)
        self.assertEqual(init_run.stdout.strip(), "initialized")

        step = self.run_cli("mcts", "step", cwd=main_dir)
        self.assertEqual(step.returncode, 0, step.stderr)
        self.assertIn("selected: root", step.stdout)
        self.assertIn("mcts/000001", step.stdout)
        self.assertIn("mcts/000002", step.stdout)

        status = self.run_cli("mcts", "status", cwd=main_dir)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("frontier_count: 3", status.stdout)
        self.assertIn("best_branch: mcts/000002", status.stdout)

        best = self.run_cli("mcts", "best", cwd=main_dir)
        self.assertEqual(best.returncode, 0, best.stderr)
        self.assertIn("mcts/000002", best.stdout)

        plot_path = self.workspace / "best-path.svg"
        plot = self.run_cli("mcts", "plot", "--output", str(plot_path), cwd=main_dir)
        self.assertEqual(plot.returncode, 0, plot.stderr)
        self.assert_output_path_matches(plot.stdout, plot_path)
        self.assertIn("branch: mcts/000002", plot.stdout)
        self.assertIn("view: lineage", plot.stdout)
        self.assertTrue(plot_path.exists())
        svg = plot_path.read_text(encoding="utf-8")
        self.assertIn("<svg", svg)
        self.assertIn("mcts/000002", svg)

        utility_plot_path = self.workspace / "best-path-utility.svg"
        utility_plot = self.run_cli(
            "mcts",
            "plot",
            "--var",
            "utility",
            "--output",
            str(utility_plot_path),
            cwd=main_dir,
        )
        self.assertEqual(utility_plot.returncode, 0, utility_plot.stderr)
        self.assert_output_path_matches(utility_plot.stdout, utility_plot_path)
        self.assertIn("var: utility", utility_plot.stdout)
        self.assertIn("view: lineage", utility_plot.stdout)
        utility_svg = utility_plot_path.read_text(encoding="utf-8")
        self.assertIn("Best-Branch Lineage: mcts/000002 | utility", utility_svg)

        branch_plot_path = self.workspace / "branch-path.svg"
        branch_plot = self.run_cli(
            "mcts",
            "plot",
            "--branch",
            "mcts/000001",
            "--output",
            str(branch_plot_path),
            cwd=main_dir,
        )
        self.assertEqual(branch_plot.returncode, 0, branch_plot.stderr)
        self.assert_output_path_matches(branch_plot.stdout, branch_plot_path)
        self.assertIn("branch: mcts/000001", branch_plot.stdout)
        self.assertIn("view: lineage", branch_plot.stdout)
        branch_svg = branch_plot_path.read_text(encoding="utf-8")
        self.assertIn("Branch Lineage: mcts/000001 | score", branch_svg)
        self.assertIn("mcts/000001", branch_svg)

        tree_plot_path = self.workspace / "search-tree.svg"
        tree_plot = self.run_cli(
            "mcts",
            "plot",
            "--view",
            "tree",
            "--output",
            str(tree_plot_path),
            cwd=main_dir,
        )
        self.assertEqual(tree_plot.returncode, 0, tree_plot.stderr)
        self.assert_output_path_matches(tree_plot.stdout, tree_plot_path)
        self.assertIn("branch: mcts/000002", tree_plot.stdout)
        self.assertIn("view: tree", tree_plot.stdout)
        tree_svg = tree_plot_path.read_text(encoding="utf-8")
        self.assertIn("Search Tree: score", tree_svg)
        self.assertIn("root", tree_svg)
        self.assertIn("mcts/000001", tree_svg)
        self.assertIn("mcts/000002", tree_svg)

    def test_cli_mcts_run_background_detaches_and_logs(self) -> None:
        main_dir = self.workspace / "main"
        main_dir.mkdir()

        init = self.run_cli("init", cwd=main_dir)
        self.assertEqual(init.returncode, 0, init.stderr)

        (main_dir / "seed.txt").write_text("root\n", encoding="utf-8")
        commit_root = self.run_cli("commit", "-m", "root base", cwd=main_dir)
        self.assertEqual(commit_root.returncode, 0, commit_root.stderr)

        config_path, _ = self.build_mcts_fixture(main_dir)
        init_run = self.run_cli("mcts", "init", "--config", str(config_path), cwd=main_dir)
        self.assertEqual(init_run.returncode, 0, init_run.stderr)

        log_path = self.workspace / "background.log"
        background = self.run_cli(
            "mcts",
            "run",
            "--steps",
            "1",
            "--background",
            "--log-file",
            str(log_path),
            cwd=main_dir,
        )
        self.assertEqual(background.returncode, 0, background.stderr)
        self.assertIn("background_pid:", background.stdout)
        self.assertIn("log_file:", background.stdout)
        reported_log_path = None
        for line in background.stdout.splitlines():
            if line.startswith("log_file: "):
                reported_log_path = Path(line.split(": ", 1)[1])
                break
        self.assertIsNotNone(reported_log_path)
        assert reported_log_path is not None
        self.assertEqual(reported_log_path.resolve(), log_path.resolve())

        deadline = time.time() + 10.0
        log_text = ""
        while time.time() < deadline:
            if log_path.exists():
                log_text = log_path.read_text(encoding="utf-8")
                if "steps_executed: 1" in log_text:
                    break
            time.sleep(0.1)
        else:
            self.fail(f"background log did not show completion in time: {log_text}")

        self.assertIn("selected: root", log_text)
        self.assertIn("mcts/000001", log_text)
        self.assertIn("mcts/000002", log_text)

        status = self.run_cli("mcts", "status", cwd=main_dir)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("steps_completed: 1", status.stdout)

    def test_cli_mcts_stop_kills_background_process_and_tmux(self) -> None:
        main_dir = self.workspace / "main"
        repo = self.init_repo_at(main_dir)
        (main_dir / "seed.txt").write_text("root\n", encoding="utf-8")
        repo.commit("root base")
        config_path, _ = self.build_mcts_fixture(main_dir)

        engine = MCTSEngine(repo)
        engine.init_search(config_path)

        state_path = main_dir / ".treegit" / "mcts-background.json"
        state_path.write_text(
            json.dumps(
                {
                    "pid": 4321,
                    "log_file": str((self.workspace / "background.log").resolve()),
                    "tmux_session": "pg-mcts",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        args = build_parser().parse_args(["mcts", "stop"])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), mock.patch("treegit.cli.Path.cwd", return_value=main_dir), mock.patch(
            "treegit.cli._terminate_process_group"
        ) as terminate, mock.patch("treegit.cli._kill_tmux_session") as kill_tmux:
            rc = run_command(args)

        self.assertEqual(rc, 0)
        terminate.assert_called_once_with(4321)
        kill_tmux.assert_called_once_with("pg-mcts")
        self.assertFalse(state_path.exists())
        self.assertEqual(engine.status()["status"], "stopped")
        self.assertIn("background_pid: 4321", stdout.getvalue())
        self.assertIn("tmux_session: pg-mcts", stdout.getvalue())

    def test_cli_reset_clears_search_tree_and_removes_worktrees(self) -> None:
        main_dir = self.workspace / "main"
        main_dir.mkdir()

        init = self.run_cli("init", cwd=main_dir)
        self.assertEqual(init.returncode, 0, init.stderr)

        (main_dir / "seed.txt").write_text("root\n", encoding="utf-8")
        commit_root = self.run_cli("commit", "-m", "root base", cwd=main_dir)
        self.assertEqual(commit_root.returncode, 0, commit_root.stderr)

        create_feature = self.run_cli("branch", "feature", cwd=main_dir)
        self.assertEqual(create_feature.returncode, 0, create_feature.stderr)
        worktree_dir = self.workspace / "feature-worktree"
        add_worktree = self.run_cli("worktree", "add", str(worktree_dir), "feature", cwd=main_dir)
        self.assertEqual(add_worktree.returncode, 0, add_worktree.stderr)
        self.assertTrue(worktree_dir.exists())

        config_path, _ = self.build_mcts_fixture(main_dir)
        init_run = self.run_cli("mcts", "init", "--config", str(config_path), cwd=main_dir)
        self.assertEqual(init_run.returncode, 0, init_run.stderr)
        first_step = self.run_cli("mcts", "step", cwd=main_dir)
        self.assertEqual(first_step.returncode, 0, first_step.stderr)

        agent1 = self.workspace / "worktrees" / "agent1"
        agent2 = self.workspace / "worktrees" / "agent2"
        self.assertTrue(agent1.exists())
        self.assertTrue(agent2.exists())

        reset = self.run_cli("reset", cwd=main_dir)
        self.assertEqual(reset.returncode, 0, reset.stderr)
        self.assertIn("reset complete", reset.stdout)
        self.assertFalse(worktree_dir.exists())
        self.assertFalse(agent1.exists())
        self.assertFalse(agent2.exists())

        branches = self.run_cli("branch", cwd=main_dir)
        self.assertEqual(branches.returncode, 0, branches.stderr)
        self.assertIn("* root", branches.stdout)
        self.assertNotIn("feature", branches.stdout)
        self.assertNotIn("mcts/000001", branches.stdout)

        status = self.run_cli("mcts", "status", cwd=main_dir)
        self.assertEqual(status.returncode, 1)
        self.assertIn("MCTS search has not been initialized", status.stderr)

    def test_cli_round_trip(self) -> None:
        result = self.run_cli("init")
        self.assertEqual(result.returncode, 0, result.stderr)

        self.write_text("notes.txt", "hello treegit\n")
        commit_root = self.run_cli("commit", "-m", "initial")
        self.assertEqual(commit_root.returncode, 0, commit_root.stderr)
        root_commit_id = commit_root.stdout.strip()

        create_branch = self.run_cli("branch", "feature")
        self.assertEqual(create_branch.returncode, 0, create_branch.stderr)

        branches = self.run_cli("branch")
        self.assertIn("* root", branches.stdout)
        self.assertIn("feature", branches.stdout)

        checkout_feature = self.run_cli("checkout", "feature", "--force")
        self.assertEqual(checkout_feature.returncode, 0, checkout_feature.stderr)

        self.write_text("notes.txt", "hello treegit\nbranch query\n")
        commit_feature = self.run_cli("commit", "-m", "feature update")
        self.assertEqual(commit_feature.returncode, 0, commit_feature.stderr)
        feature_commit_id = commit_feature.stdout.strip()

        search = self.run_cli("search", "query", "--field", "content", "--branch", "feature")
        self.assertEqual(search.returncode, 0, search.stderr)
        self.assertIn("content:", search.stdout)
        self.assertIn("notes.txt", search.stdout)

        diff = self.run_cli("diff", root_commit_id, feature_commit_id)
        self.assertEqual(diff.returncode, 0, diff.stderr)
        self.assertIn("+branch query", diff.stdout)

        checkout_root_again = self.run_cli("checkout", "root", "--force")
        self.assertEqual(checkout_root_again.returncode, 0, checkout_root_again.stderr)
        self.assertEqual((self.workspace / "notes.txt").read_text(encoding="utf-8"), "hello treegit\n")

        log = self.run_cli("log")
        self.assertEqual(log.returncode, 0, log.stderr)
        self.assertIn("initial", log.stdout)

    def test_cli_worktree_add_creates_independent_local_branch_binding(self) -> None:
        main_dir = self.workspace / "main"
        feature_dir = self.workspace / "feature"
        main_dir.mkdir()

        result = self.run_cli("init", cwd=main_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

        (main_dir / "notes.txt").write_text("root\n", encoding="utf-8")
        commit_root = self.run_cli("commit", "-m", "initial", cwd=main_dir)
        self.assertEqual(commit_root.returncode, 0, commit_root.stderr)

        create_branch = self.run_cli("branch", "feature", cwd=main_dir)
        self.assertEqual(create_branch.returncode, 0, create_branch.stderr)

        add_worktree = self.run_cli("worktree", "add", str(feature_dir), "feature", cwd=main_dir)
        self.assertEqual(add_worktree.returncode, 0, add_worktree.stderr)
        self.assertEqual((feature_dir / "notes.txt").read_text(encoding="utf-8"), "root\n")

        feature_branch = self.run_cli("branch", cwd=feature_dir)
        self.assertEqual(feature_branch.returncode, 0, feature_branch.stderr)
        self.assertIn("*   feature", feature_branch.stdout)

        main_branch = self.run_cli("branch", cwd=main_dir)
        self.assertEqual(main_branch.returncode, 0, main_branch.stderr)
        self.assertIn("* root", main_branch.stdout)

    def test_cli_worktree_add_rebinds_existing_worktree_folder(self) -> None:
        main_dir = self.workspace / "main"
        feature_dir = self.workspace / "feature"
        main_dir.mkdir()

        result = self.run_cli("init", cwd=main_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

        (main_dir / "notes.txt").write_text("root\n", encoding="utf-8")
        commit_root = self.run_cli("commit", "-m", "initial", cwd=main_dir)
        self.assertEqual(commit_root.returncode, 0, commit_root.stderr)

        create_feature = self.run_cli("branch", "feature", cwd=main_dir)
        self.assertEqual(create_feature.returncode, 0, create_feature.stderr)
        create_alt = self.run_cli("branch", "alt", cwd=main_dir)
        self.assertEqual(create_alt.returncode, 0, create_alt.stderr)

        add_worktree = self.run_cli("worktree", "add", str(feature_dir), "feature", cwd=main_dir)
        self.assertEqual(add_worktree.returncode, 0, add_worktree.stderr)
        (feature_dir / "notes.txt").write_text("feature\n", encoding="utf-8")
        commit_feature = self.run_cli("commit", "-m", "feature update", cwd=feature_dir)
        self.assertEqual(commit_feature.returncode, 0, commit_feature.stderr)

        rebind_worktree = self.run_cli("worktree", "add", str(feature_dir), "alt", cwd=main_dir)
        self.assertEqual(rebind_worktree.returncode, 0, rebind_worktree.stderr)
        self.assertEqual((feature_dir / "notes.txt").read_text(encoding="utf-8"), "root\n")

        feature_branch = self.run_cli("branch", cwd=feature_dir)
        self.assertEqual(feature_branch.returncode, 0, feature_branch.stderr)
        self.assertIn("*   alt", feature_branch.stdout)

    def test_cli_metric_define_get_and_backprop(self) -> None:
        result = self.run_cli("init")
        self.assertEqual(result.returncode, 0, result.stderr)

        self.write_text("notes.txt", "root\n")
        commit_root = self.run_cli("commit", "-m", "initial")
        self.assertEqual(commit_root.returncode, 0, commit_root.stderr)

        define_metric = self.run_cli("metric", "define", "score")
        self.assertEqual(define_metric.returncode, 0, define_metric.stderr)

        root_score = self.run_cli("metric", "get", "score")
        self.assertEqual(root_score.returncode, 0, root_score.stderr)
        self.assertEqual(float(root_score.stdout.strip()), 0.0)

        create_feature = self.run_cli("branch", "feature")
        self.assertEqual(create_feature.returncode, 0, create_feature.stderr)

        checkout_feature = self.run_cli("checkout", "feature", "--force")
        self.assertEqual(checkout_feature.returncode, 0, checkout_feature.stderr)

        create_leaf = self.run_cli("branch", "leaf")
        self.assertEqual(create_leaf.returncode, 0, create_leaf.stderr)

        checkout_leaf = self.run_cli("checkout", "leaf", "--force")
        self.assertEqual(checkout_leaf.returncode, 0, checkout_leaf.stderr)

        leaf_score = self.run_cli("metric", "get", "score")
        self.assertEqual(leaf_score.returncode, 0, leaf_score.stderr)
        self.assertEqual(float(leaf_score.stdout.strip()), 0.0)

        backprop_score = self.run_cli("metric", "backprop", "score", "2.5")
        self.assertEqual(backprop_score.returncode, 0, backprop_score.stderr)

        updated_leaf_score = self.run_cli("metric", "get", "score")
        self.assertEqual(updated_leaf_score.returncode, 0, updated_leaf_score.stderr)
        self.assertEqual(float(updated_leaf_score.stdout.strip()), 2.5)

        checkout_root = self.run_cli("checkout", "root", "--force")
        self.assertEqual(checkout_root.returncode, 0, checkout_root.stderr)

        updated_root_score = self.run_cli("metric", "get", "score")
        self.assertEqual(updated_root_score.returncode, 0, updated_root_score.stderr)
        self.assertEqual(float(updated_root_score.stdout.strip()), 2.5)

        create_alt = self.run_cli("branch", "alt")
        self.assertEqual(create_alt.returncode, 0, create_alt.stderr)

        checkout_alt = self.run_cli("checkout", "alt", "--force")
        self.assertEqual(checkout_alt.returncode, 0, checkout_alt.stderr)

        alt_score = self.run_cli("metric", "get", "score")
        self.assertEqual(alt_score.returncode, 0, alt_score.stderr)
        self.assertEqual(float(alt_score.stdout.strip()), 0.0)


if __name__ == "__main__":
    unittest.main()
