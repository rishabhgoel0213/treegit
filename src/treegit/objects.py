from __future__ import annotations

import json
from dataclasses import asdict

from treegit.hashing import text_bytes
from treegit.models import CommitRecord, ObjectRecord, TreeEntry


def serialize_blob(raw: bytes) -> bytes:
    return raw


def serialize_tree(entries: list[TreeEntry]) -> bytes:
    payload = []
    for entry in sorted(entries, key=lambda item: item.name):
        payload.append([entry.name, entry.mode, entry.kind, entry.object_id])
    return text_bytes(json.dumps(payload, separators=(",", ":"), ensure_ascii=True))


def serialize_commit(parent_id: str | None, root_tree_id: str, message: str, created_at: str) -> bytes:
    payload = {
        "created_at": created_at,
        "message": message,
        "parent": parent_id,
        "root_tree": root_tree_id,
    }
    return text_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def decode_object(raw: bytes) -> ObjectRecord:
    marker, _, payload = raw.partition(b"\n")
    return ObjectRecord(kind=marker.decode("utf-8"), payload=payload)


def parse_tree(payload: bytes) -> list[TreeEntry]:
    rows = json.loads(payload.decode("utf-8"))
    return [TreeEntry(name=name, mode=mode, kind=kind, object_id=object_id) for name, mode, kind, object_id in rows]


def parse_commit(commit_id: str, payload: bytes) -> CommitRecord:
    data = json.loads(payload.decode("utf-8"))
    return CommitRecord(
        commit_id=commit_id,
        parent_id=data.get("parent"),
        root_tree_id=data["root_tree"],
        message=data["message"],
        created_at=data["created_at"],
    )
