from __future__ import annotations

import os
import zlib
from pathlib import Path

from treegit.errors import InvalidObjectError
from treegit.hashing import object_id


class ObjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.objects_dir = root / "objects"

    def init(self) -> None:
        self.objects_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, oid: str) -> Path:
        return self.objects_dir / oid[:2] / oid[2:]

    def has_object(self, oid: str) -> bool:
        return self.path_for(oid).exists()

    def write_object(self, kind: str, payload: bytes) -> str:
        oid = object_id(kind, payload)
        target = self.path_for(oid)
        if target.exists():
            return oid
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
        compressed = zlib.compress(kind.encode("utf-8") + b"\n" + payload)
        temp.write_bytes(compressed)
        os.replace(temp, target)
        return oid

    def read_object(self, oid: str) -> tuple[str, bytes]:
        target = self.path_for(oid)
        if not target.exists():
            raise InvalidObjectError(f"missing object {oid}")
        raw = zlib.decompress(target.read_bytes())
        kind, _, payload = raw.partition(b"\n")
        if not kind:
            raise InvalidObjectError(f"invalid object {oid}")
        return kind.decode("utf-8"), payload
