from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
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
        with self.assertRaises(InvalidObjectError):
            repo.checkout("backup", force=True)

        self.write_text("tracked.txt", "updated\n")
        stable_head = repo.head_commit_id()
        with mock.patch.object(repo.index, "write_commit", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                repo.commit("interrupted")
        self.assertEqual(repo.head_commit_id(), stable_head)

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
