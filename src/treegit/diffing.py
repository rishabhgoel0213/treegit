from __future__ import annotations

import difflib

from treegit.models import FileSnapshot, WorkingFile


def render_text_diff(path: str, old: str, new: str) -> str:
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(lines)


def binary_diff_summary(path: str) -> str:
    return f"Binary files differ: {path}\n"
