from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path

from treegit.diffing import binary_diff_summary, render_text_diff
from treegit.errors import (
    BranchExistsError,
    BranchNavigationError,
    CheckoutConflictError,
    DirtyWorkingTreeError,
    InvalidObjectError,
    MetricExistsError,
    MetricNotFoundError,
    ReferenceResolutionError,
    RepoExistsError,
    RepoNotFoundError,
)
from treegit.index import MetadataIndex
from treegit.models import BranchRecord, CommitRecord, FileSnapshot, SearchResult, TreeEntry, WorkingFile
from treegit.objects import serialize_blob, serialize_commit, serialize_tree
from treegit.store import ObjectStore
from treegit.working_tree import read_working_file_raw, scan_working_tree


MAX_PREFIX = 8


@dataclass(frozen=True)
class StatusReport:
    added: list[str]
    modified: list[str]
    deleted: list[str]
    untracked: list[str]

    def is_dirty(self) -> bool:
        return any((self.added, self.modified, self.deleted, self.untracked))


class Repository:
    def __init__(self, root: Path, git_dir: Path | None = None, common_dir: Path | None = None) -> None:
        self.root = root
        self.git_dir = git_dir or (root / ".treegit")
        self.common_dir = common_dir or self.git_dir
        self.store = ObjectStore(self.common_dir)
        self.index = MetadataIndex(self.common_dir / "index.db")

    @classmethod
    def init(cls, root: Path) -> "Repository":
        repo = cls(root)
        if repo.git_dir.exists():
            raise RepoExistsError(f"repository already exists at {repo.git_dir}")
        repo.git_dir.mkdir(parents=True, exist_ok=False)
        repo.store.init()
        repo.index.init()
        repo._write_branch("root")
        return repo

    @classmethod
    def discover(cls, start: Path) -> "Repository":
        current = start.resolve()
        for candidate in [current, *current.parents]:
            git_dir = candidate / ".treegit"
            if git_dir.is_dir():
                repo = cls(candidate, git_dir=git_dir, common_dir=cls._resolve_common_dir(git_dir))
                repo.index.init()
                return repo
        raise RepoNotFoundError("not inside a TreeGit repository")

    def current_branch(self) -> str | None:
        branch = self._read_branch()
        if branch is not None:
            return branch
        kind, target = self.index.read_head()
        return target if kind == "branch" else None

    def head_commit_id(self) -> str | None:
        branch = self.current_branch()
        if branch is not None:
            return self.index.get_ref(branch)
        kind, target = self.index.read_head()
        if kind == "branch":
            return self.index.get_ref(target)
        return target or None

    def head_commit(self) -> CommitRecord | None:
        commit_id = self.head_commit_id()
        if commit_id is None:
            return None
        commit = self.index.get_commit(commit_id)
        if commit is None:
            raise InvalidObjectError(f"missing commit metadata for {commit_id}")
        return commit

    def resolve_revision(self, value: str) -> str:
        if value == "HEAD":
            commit_id = self.head_commit_id()
            if commit_id is None:
                raise ReferenceResolutionError("HEAD does not point to a commit")
            return commit_id
        if self.index.has_ref(value):
            ref_value = self.index.get_ref(value)
            if ref_value is None:
                raise ReferenceResolutionError(f"branch {value} has no commits")
            return ref_value
        if len(value) < MAX_PREFIX:
            raise ReferenceResolutionError("commit prefix must be at least 8 characters")
        matches = self._matching_commits(value)
        if not matches:
            raise ReferenceResolutionError(f"unknown revision {value}")
        if len(matches) > 1:
            raise ReferenceResolutionError(f"ambiguous commit prefix {value}")
        return matches[0]

    def _matching_commits(self, prefix: str) -> list[str]:
        conn = self.index.connect()
        try:
            rows = conn.execute(
                "SELECT commit_id FROM commits WHERE commit_id LIKE ? ORDER BY commit_id",
                (f"{prefix}%",),
            ).fetchall()
            return [row["commit_id"] for row in rows]
        finally:
            conn.close()

    def list_branches(self) -> list[BranchRecord]:
        return self.index.list_branches()

    def create_branch(self, name: str) -> None:
        self.create_branch_from(name=name, parent_name=self.current_branch(), commit_id=self.head_commit_id())

    def create_branch_from(self, name: str, parent_name: str | None, commit_id: str | None) -> None:
        if self.index.has_ref(name):
            raise BranchExistsError(f"branch {name} already exists")
        if parent_name is not None and self.index.get_branch(parent_name) is None:
            raise ReferenceResolutionError(f"unknown parent branch {parent_name}")
        self.index.create_branch(
            name=name,
            commit_id=commit_id,
            parent_name=parent_name,
            fork_commit_id=commit_id,
        )
        if commit_id is None:
            self.index.replace_branch_tip(name, [])
            return
        source_files = list(self._file_map_for_commit(commit_id).values())
        self.index.replace_branch_tip(name, source_files)

    def define_metric(self, name: str) -> None:
        if self.index.has_metric(name):
            raise MetricExistsError(f"metric {name} already exists")
        self.index.define_metric(name, default=0.0)

    def get_metric(self, name: str) -> float:
        branch_name = self.current_branch()
        if branch_name is None:
            raise ReferenceResolutionError("metric operations require a branch checkout")
        value = self.index.get_branch_metric(branch_name, name)
        if value is None:
            raise MetricNotFoundError(f"unknown metric {name}")
        return value

    def backprop_metric(self, name: str, value: float) -> None:
        branch_name = self.current_branch()
        if branch_name is None:
            raise ReferenceResolutionError("metric operations require a branch checkout")
        if not self.index.has_metric(name):
            raise MetricNotFoundError(f"unknown metric {name}")
        lineage: list[str] = []
        seen: set[str] = set()
        current_name: str | None = branch_name
        while current_name is not None and current_name not in seen:
            seen.add(current_name)
            lineage.append(current_name)
            branch = self.index.get_branch(current_name)
            if branch is None:
                raise ReferenceResolutionError(f"unknown branch {current_name}")
            current_name = branch.parent_name
        self.index.increment_metric_for_branches(name, lineage, value)

    def _scan_working_tree(self) -> dict[str, WorkingFile]:
        return scan_working_tree(self.root, self.git_dir)

    def status(self) -> StatusReport:
        head_files = self._head_file_map()
        working_files = self._scan_working_tree()
        if not head_files:
            return StatusReport(added=[], modified=[], deleted=[], untracked=list(working_files))
        added: list[str] = []
        modified: list[str] = []
        deleted: list[str] = []
        untracked: list[str] = []
        for path, work_file in working_files.items():
            snapshot = head_files.get(path)
            if snapshot is None:
                added.append(path)
            elif snapshot.blob_id != work_file.blob_id or snapshot.mode != work_file.mode:
                modified.append(path)
        for path in head_files:
            if path not in working_files:
                deleted.append(path)
        return StatusReport(
            added=sorted(added),
            modified=sorted(modified),
            deleted=sorted(deleted),
            untracked=sorted(untracked),
        )

    def commit(self, message: str) -> str:
        working_files = self._scan_working_tree()
        parent = self.head_commit()
        parent_files = self._head_file_map()
        changed_paths, deleted_paths = self._working_tree_changes(working_files, parent_files)
        if parent is not None and not changed_paths and not deleted_paths:
            tree_id = parent.root_tree_id
            files: list[FileSnapshot] = []
            blob_contents: dict[str, str] = {}
        else:
            reusable_parent_paths = set(working_files) - changed_paths
            changed_text_blobs = {
                working_files[path].blob_id
                for path in changed_paths
                if working_files[path].is_text
            }
            unindexed_text_blobs = self.index.unindexed_text_blob_ids(changed_text_blobs)
            tree_id, files, blob_contents = self._write_tree_from_working_files(
                working_files,
                reusable_parent_paths=reusable_parent_paths,
                unindexed_text_blobs=unindexed_text_blobs,
                snapshot_paths=changed_paths,
            )
        created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        payload = serialize_commit(parent.commit_id if parent else None, tree_id, message, created_at)
        commit_id = self.store.write_object("commit", payload)
        commit = CommitRecord(
            commit_id=commit_id,
            parent_id=parent.commit_id if parent else None,
            root_tree_id=tree_id,
            message=message,
            created_at=created_at,
        )
        branch = self.current_branch()
        self.index.write_commit(
            commit,
            files,
            branch_name=branch,
            blob_contents=blob_contents,
            parent_commit_id=parent.commit_id if parent else None,
            deleted_paths=deleted_paths,
        )
        if branch is None:
            self.index.update_head("detached", commit_id)
        return commit_id

    def log(self, revision: str | None = None) -> list[CommitRecord]:
        start = self.resolve_revision(revision) if revision else self.head_commit_id()
        if start is None:
            return []
        records: list[CommitRecord] = []
        current = start
        while current:
            commit = self.index.get_commit(current)
            if commit is None:
                raise InvalidObjectError(f"missing commit metadata for {current}")
            records.append(commit)
            current = commit.parent_id
        return records

    def checkout(self, revision: str, force: bool = False) -> str:
        target_branch = self.index.get_branch(revision)
        if target_branch is None:
            raise ReferenceResolutionError("checkout only supports branch names")
        return self._switch_branch(target_branch, force=force, require_navigation=True)

    def add_worktree(self, path: Path, branch: str) -> "Repository":
        target_branch = self.index.get_branch(branch)
        if target_branch is None:
            raise ReferenceResolutionError(f"unknown branch {branch}")
        target_root = path.resolve()
        if target_root.exists():
            if not target_root.is_dir():
                raise RepoExistsError(f"worktree path is not a directory: {target_root}")
        else:
            target_root.mkdir(parents=True, exist_ok=False)
        target_git_dir = target_root / ".treegit"
        if target_git_dir.exists():
            repo = Repository(target_root, git_dir=target_git_dir, common_dir=self._resolve_common_dir(target_git_dir))
            if repo.common_dir.resolve() != self.common_dir.resolve():
                raise RepoExistsError(f"worktree path belongs to a different repository: {target_root}")
            repo._switch_branch(target_branch, force=False, require_navigation=False)
            return repo
        if any(target_root.iterdir()):
            raise RepoExistsError(f"worktree path is not empty: {target_root}")
        target_git_dir.mkdir(parents=True, exist_ok=False)
        (target_git_dir / "commondir").write_text(str(self.common_dir.resolve()), encoding="utf-8")
        repo = Repository(target_root, git_dir=target_git_dir, common_dir=self.common_dir)
        repo._write_branch(target_branch.name)
        repo._materialize_branch(target_branch)
        return repo

    def diff(self, left: str | None = None, right: str | None = None) -> str:
        if left is None and right is None:
            base = self._head_file_map()
            compare = self._scan_working_tree()
            return self._render_diff(base, compare)
        if left is not None and right is None:
            base = self._file_map_for_commit(self.resolve_revision(left))
            compare = self._scan_working_tree()
            return self._render_diff(base, compare)
        assert left is not None and right is not None
        base = self._file_map_for_commit(self.resolve_revision(left))
        compare = self._file_map_for_commit(self.resolve_revision(right))
        return self._render_diff(base, compare)

    def search(
        self,
        query: str,
        field: str = "all",
        branch: str | None = None,
        path_glob: str | None = None,
        limit: int = 20,
    ) -> dict[str, list[SearchResult]]:
        branch_names = [branch] if branch else None
        reachable_by_branch = self.index.reachable_commits(branch_names=branch_names)
        reachable = set().union(*reachable_by_branch.values()) if reachable_by_branch else set()
        results: dict[str, list[SearchResult]] = defaultdict(list)
        if field in {"branch", "all"}:
            for name in self.index.search_branches(query, limit):
                results["branch"].append(SearchResult("branch", name, None, name, None))
        if field in {"commit", "all"}:
            for commit in self.index.search_commits(query, reachable, limit):
                results["commit"].append(
                    SearchResult("commit", commit.commit_id, None, commit.message, commit.created_at)
                )
        if field in {"path", "content", "all"}:
            self._ensure_commit_manifests(reachable)
        if field in {"path", "all"}:
            for row in self.index.search_paths(query, reachable, path_glob, limit):
                results["path"].append(
                    SearchResult("path", row["commit_id"], row["path"], row["path"], row["created_at"])
                )
        if field in {"content", "all"}:
            for row in self.index.search_content(query, reachable, path_glob, limit):
                snippet = self._content_snippet(row["content"], query)
                results["content"].append(
                    SearchResult("content", row["commit_id"], row["path"], snippet, row["created_at"])
                )
        return dict(results)

    def _content_snippet(self, content: str, query: str) -> str:
        lowered = content.lower()
        token = query.split()[0].lower()
        index = lowered.find(token)
        if index == -1:
            return content.splitlines()[0] if content else ""
        start = max(0, index - 30)
        end = min(len(content), index + 80)
        return content[start:end].replace("\n", " ")

    def _head_file_map(self) -> dict[str, FileSnapshot]:
        branch_name = self.current_branch()
        if branch_name is None:
            commit_id = self.head_commit_id()
            if commit_id is None:
                return {}
            return self._file_map_for_commit(commit_id)
        commit_id = self.index.get_ref(branch_name)
        return self._branch_file_map(branch_name, commit_id)

    @staticmethod
    def _resolve_common_dir(git_dir: Path) -> Path:
        commondir_file = git_dir / "commondir"
        if not commondir_file.exists():
            return git_dir
        raw_path = commondir_file.read_text(encoding="utf-8").strip()
        if not raw_path:
            raise InvalidObjectError(f"invalid commondir file at {commondir_file}")
        common_dir = Path(raw_path)
        if not common_dir.is_absolute():
            common_dir = (git_dir / common_dir).resolve()
        return common_dir

    def _branch_file(self) -> Path:
        return self.git_dir / "BRANCH"

    def _read_branch(self) -> str | None:
        branch_file = self._branch_file()
        if not branch_file.exists():
            return None
        branch = branch_file.read_text(encoding="utf-8").strip()
        return branch or None

    def _write_branch(self, branch: str) -> None:
        self._branch_file().write_text(f"{branch}\n", encoding="utf-8")

    def _switch_branch(self, target_branch: BranchRecord, force: bool, require_navigation: bool) -> str:
        status = self.status()
        dirty_paths = status.added + status.modified + status.deleted + status.untracked
        if dirty_paths and not force:
            raise DirtyWorkingTreeError("working tree is dirty")
        if require_navigation and not self._can_checkout_branch(target_branch):
            raise BranchNavigationError(
                f"can only checkout the parent or a direct child branch from {self.current_branch() or 'HEAD'}"
            )
        target_commit_id = self._materialize_branch(target_branch)
        return target_commit_id or ""

    def _materialize_branch(self, target_branch: BranchRecord) -> str | None:
        target_commit_id = target_branch.commit_id
        target_files = self._branch_file_map(target_branch.name, target_commit_id)
        current_files = self._scan_working_tree()
        tracked_files = self._head_file_map()
        conflicts = []
        for path in target_files:
            if path not in tracked_files and path in current_files:
                conflicts.append(path)
                continue
            ancestor = self._first_untracked_ancestor(path, current_files, tracked_files)
            if ancestor is not None:
                conflicts.append(ancestor)
        if conflicts:
            unique_conflicts = ", ".join(sorted(set(conflicts)))
            raise CheckoutConflictError(f"checkout would overwrite untracked files: {unique_conflicts}")
        self._materialize_tree(target_files, tracked_files, current_files)
        self._write_branch(target_branch.name)
        return target_commit_id

    def _file_map_for_commit(self, commit_id: str) -> dict[str, FileSnapshot]:
        self._ensure_commit_manifest(commit_id)
        return {item.path: item for item in self.index.files_for_commit(commit_id)}

    def _branch_file_map(self, branch_name: str, commit_id: str | None) -> dict[str, FileSnapshot]:
        tip_files = self.index.branch_tip_files(branch_name)
        if tip_files or commit_id is None:
            return {item.path: item for item in tip_files}
        files = self._file_map_for_commit(commit_id)
        self.index.replace_branch_tip(branch_name, list(files.values()))
        return files

    def _can_checkout_branch(self, target_branch: BranchRecord) -> bool:
        current_name = self.current_branch()
        if current_name is None:
            return target_branch.parent_name is None
        if target_branch.name == current_name:
            return True
        if target_branch.parent_name is None:
            return True
        current_branch = self.index.get_branch(current_name)
        if current_branch is None:
            return False
        if target_branch.parent_name == current_name:
            return True
        return current_branch.parent_name == target_branch.name

    def _write_tree_from_working_files(
        self,
        working_files: dict[str, WorkingFile],
        reusable_parent_paths: set[str] | None = None,
        unindexed_text_blobs: set[str] | None = None,
        snapshot_paths: set[str] | None = None,
    ) -> tuple[str, list[FileSnapshot], dict[str, str]]:
        reusable_paths = set() if reusable_parent_paths is None else reusable_parent_paths
        pending_text_blobs = set() if unindexed_text_blobs is None else unindexed_text_blobs
        directories: dict[tuple[str, ...], list[TreeEntry]] = defaultdict(list)
        all_directories: set[tuple[str, ...]] = {()}
        snapshots: list[FileSnapshot] = []
        blob_contents: dict[str, str] = {}
        for path, item in working_files.items():
            blob_id, raw = self._ensure_working_blob(item, reuse_cached_blob=path in reusable_paths)
            if snapshot_paths is None or path in snapshot_paths:
                snapshots.append(
                    FileSnapshot(
                        path=path,
                        mode=item.mode,
                        blob_id=blob_id,
                        size=item.size,
                        is_text=item.is_text,
                    )
                )
            if item.is_text and blob_id in pending_text_blobs and blob_id not in blob_contents:
                payload = raw if raw is not None else self._read_blob_payload(blob_id)
                blob_contents[blob_id] = payload.decode("utf-8")
            parts = tuple(path.split("/"))
            parent = parts[:-1]
            for size in range(len(parent) + 1):
                all_directories.add(parent[:size])
            directories[parent].append(
                TreeEntry(
                    name=parts[-1],
                    mode=item.mode,
                    kind="blob",
                    object_id=blob_id,
                )
            )
        known_paths = sorted(all_directories, key=len, reverse=True)
        for directory in known_paths:
            if not directory:
                continue
            tree_id = self._write_tree(directory, directories[directory])
            parent = directory[:-1]
            directories[parent].append(
                TreeEntry(name=directory[-1], mode="040000", kind="tree", object_id=tree_id)
            )
        root_tree_id = self._write_tree((), directories.get((), []))
        return root_tree_id, sorted(snapshots, key=lambda item: item.path), blob_contents

    def _ensure_working_blob(self, item: WorkingFile, reuse_cached_blob: bool = False) -> tuple[str, bytes | None]:
        if item.raw is not None:
            return self.store.write_object("blob", serialize_blob(item.raw)), item.raw
        if reuse_cached_blob:
            return item.blob_id, None
        if self.store.has_object(item.blob_id):
            return item.blob_id, None
        raw = read_working_file_raw(self.root / item.path, item.path)
        return self.store.write_object("blob", serialize_blob(raw)), raw

    def _working_tree_changes(
        self,
        working_files: dict[str, WorkingFile],
        parent_files: dict[str, FileSnapshot],
    ) -> tuple[set[str], set[str]]:
        changed_paths = {
            path
            for path, item in working_files.items()
            if (snapshot := parent_files.get(path)) is None
            or snapshot.blob_id != item.blob_id
            or snapshot.mode != item.mode
        }
        deleted_paths = set(parent_files) - set(working_files)
        return changed_paths, deleted_paths

    def _write_tree(self, directory: tuple[str, ...], entries: list[TreeEntry]) -> str:
        payload = serialize_tree(entries)
        return self.store.write_object("tree", payload)

    def _first_untracked_ancestor(
        self,
        path: str,
        current_files: dict[str, WorkingFile],
        tracked_files: dict[str, FileSnapshot],
    ) -> str | None:
        parts = path.split("/")
        for size in range(1, len(parts)):
            candidate = "/".join(parts[:size])
            if candidate in current_files and candidate not in tracked_files:
                return candidate
        return None

    def _materialize_tree(
        self,
        target_files: dict[str, FileSnapshot],
        tracked_files: dict[str, FileSnapshot],
        current_files: dict[str, WorkingFile],
    ) -> None:
        for path in tracked_files:
            if path not in target_files:
                absolute = self.root / path
                if absolute.exists() or absolute.is_symlink():
                    if absolute.is_dir():
                        continue
                    absolute.unlink()
                    self._cleanup_empty_parents(absolute.parent)
        for path, snapshot in target_files.items():
            current = current_files.get(path)
            if (
                current is not None
                and path in tracked_files
                and current.blob_id == snapshot.blob_id
                and current.mode == snapshot.mode
            ):
                continue
            absolute = self.root / path
            absolute.parent.mkdir(parents=True, exist_ok=True)
            kind, payload = self.store.read_object(snapshot.blob_id)
            if kind != "blob":
                raise InvalidObjectError(f"invalid blob object {snapshot.blob_id}")
            if absolute.exists() or absolute.is_symlink():
                if absolute.is_dir():
                    raise CheckoutConflictError(f"cannot overwrite directory {path}")
                absolute.unlink()
            if snapshot.mode == "120000":
                os.symlink(payload.decode("utf-8"), absolute)
            else:
                absolute.write_bytes(payload)
                permissions = 0o755 if snapshot.mode == "100755" else 0o644
                absolute.chmod(permissions)

    def _cleanup_empty_parents(self, path: Path) -> None:
        while path != self.root and path.exists():
            try:
                path.rmdir()
            except OSError:
                return
            path = path.parent

    def _render_diff(
        self,
        left: dict[str, FileSnapshot] | dict[str, WorkingFile],
        right: dict[str, FileSnapshot] | dict[str, WorkingFile],
    ) -> str:
        output: list[str] = []
        paths = sorted(set(left) | set(right))
        for path in paths:
            left_item = left.get(path)
            right_item = right.get(path)
            if left_item is None:
                output.append(self._diff_added(path, right_item))
                continue
            if right_item is None:
                output.append(self._diff_deleted(path, left_item))
                continue
            if left_item.blob_id == right_item.blob_id and left_item.mode == right_item.mode:
                continue
            left_blob, left_mode, left_text = self._blob_contents(left_item)
            right_blob, right_mode, right_text = self._blob_contents(right_item)
            if left_blob == right_blob and left_mode == right_mode:
                continue
            if left_mode != right_mode:
                output.append(f"Mode changed: {path} {left_mode} -> {right_mode}\n")
            if left_text is not None and right_text is not None:
                output.append(render_text_diff(path, left_text, right_text))
            else:
                output.append(binary_diff_summary(path))
        return "".join(output)

    def _diff_added(self, path: str, item: FileSnapshot | WorkingFile | None) -> str:
        if item is None:
            return ""
        _, _, text = self._blob_contents(item)
        if text is not None:
            return render_text_diff(path, "", text)
        return binary_diff_summary(path)

    def _diff_deleted(self, path: str, item: FileSnapshot | WorkingFile) -> str:
        _, _, text = self._blob_contents(item)
        if text is not None:
            return render_text_diff(path, text, "")
        return binary_diff_summary(path)

    def _blob_contents(self, item: FileSnapshot | WorkingFile) -> tuple[bytes, str, str | None]:
        if isinstance(item, WorkingFile):
            if item.raw is not None:
                raw = item.raw
            elif self.store.has_object(item.blob_id):
                raw = self._read_blob_payload(item.blob_id)
            else:
                raw = read_working_file_raw(self.root / item.path, item.path)
            mode = item.mode
            return raw, mode, raw.decode("utf-8") if item.is_text else None
        payload = self._read_blob_payload(item.blob_id)
        return payload, item.mode, payload.decode("utf-8") if item.is_text else None

    def _read_blob_payload(self, blob_id: str) -> bytes:
        kind, payload = self.store.read_object(blob_id)
        if kind != "blob":
            raise InvalidObjectError(f"invalid blob object {blob_id}")
        return payload

    def _ensure_commit_manifests(self, commit_ids: set[str]) -> None:
        ensured: set[str] = set()
        for commit_id in sorted(commit_ids):
            self._ensure_commit_manifest(commit_id, ensured)

    def _ensure_commit_manifest(self, commit_id: str, ensured: set[str] | None = None) -> None:
        if ensured is not None and commit_id in ensured:
            return
        if self.index.commit_manifest_ready(commit_id):
            if ensured is not None:
                ensured.add(commit_id)
            return
        commit = self.index.get_commit(commit_id)
        if commit is None:
            raise InvalidObjectError(f"missing commit metadata for {commit_id}")
        manifest: dict[str, FileSnapshot] = {}
        if commit.parent_id is not None:
            self._ensure_commit_manifest(commit.parent_id, ensured)
            manifest = {item.path: item for item in self.index.files_for_commit(commit.parent_id)}
        changed_files, deleted_paths = self.index.commit_changes(commit_id)
        for path in deleted_paths:
            manifest.pop(path, None)
        for item in changed_files:
            manifest[item.path] = item
        self.index.store_commit_manifest(commit_id, sorted(manifest.values(), key=lambda item: item.path))
        if ensured is not None:
            ensured.add(commit_id)
