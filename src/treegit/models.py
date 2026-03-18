from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TreeEntry:
    name: str
    mode: str
    kind: str
    object_id: str


@dataclass(frozen=True)
class ObjectRecord:
    kind: str
    payload: bytes


@dataclass(frozen=True)
class CommitRecord:
    commit_id: str
    parent_id: str | None
    root_tree_id: str
    message: str
    created_at: str


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    mode: str
    blob_id: str
    size: int
    is_text: bool


@dataclass(frozen=True)
class WorkingFile:
    path: str
    mode: str
    raw: bytes
    size: int
    is_text: bool
    blob_id: str


@dataclass(frozen=True)
class SearchResult:
    category: str
    ref: str
    path: str | None
    summary: str
    created_at: str | None


@dataclass(frozen=True)
class BranchRecord:
    name: str
    commit_id: str | None
    parent_name: str | None
    fork_commit_id: str | None
