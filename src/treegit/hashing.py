from __future__ import annotations

import hashlib


def object_id(kind: str, payload: bytes) -> str:
    header = f"{kind}\n".encode("utf-8")
    return hashlib.sha256(header + payload).hexdigest()


def text_bytes(value: str) -> bytes:
    return value.encode("utf-8")
