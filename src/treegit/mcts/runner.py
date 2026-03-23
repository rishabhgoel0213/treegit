from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Mapping

from treegit.errors import MCTSConfigError, MCTSExecutionError
from treegit.mcts.models import CommandSpec


def run_command(spec: CommandSpec, context: Mapping[str, str], default_cwd: Path) -> subprocess.CompletedProcess[str]:
    command = [_render_template(item, context) for item in spec.command]
    cwd = default_cwd
    if spec.cwd is not None:
        cwd = Path(_render_template(spec.cwd, context)).expanduser().resolve()
    env = os.environ.copy()
    env.update({key: value for key, value in context.items() if key.isupper()})
    env.update({key: _render_template(value, context) for key, value in spec.env.items()})
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=spec.timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise MCTSExecutionError(f"command not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MCTSExecutionError(f"command timed out after {spec.timeout_seconds}s: {' '.join(command)}") from exc


def _render_template(value: str, context: Mapping[str, str]) -> str:
    try:
        return value.format(**context)
    except KeyError as exc:
        raise MCTSConfigError(f"unknown template variable {exc.args[0]!r} in command config") from exc
