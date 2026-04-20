#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import shlex
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Codex as a TreeGit policy adapter.")
    parser.add_argument("--prompt-file", type=Path, required=True, help="Prompt template file.")
    parser.add_argument("--model", default=None, help="Optional Codex model override.")
    parser.add_argument(
        "--reasoning-effort",
        choices=("low", "medium", "high", "xhigh"),
        default=None,
        help="Optional Codex reasoning effort override.",
    )
    parser.add_argument(
        "--tmux",
        action="store_true",
        help="Run Codex inside a detached tmux session and wait for it to finish.",
    )
    parser.add_argument(
        "--tmux-session-prefix",
        default="treegit-codex",
        help="Session name prefix to use when --tmux is enabled.",
    )
    parser.add_argument(
        "--unsafe",
        action="store_true",
        help="Pass --dangerously-bypass-approvals-and-sandbox to codex exec.",
    )
    parser.add_argument(
        "--cleanup-path",
        action="append",
        default=[],
        help="Relative path to remove from the worktree after Codex exits and before TreeGit commits.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    worktree = Path(_require_env("TREEGIT_WORKTREE")).resolve()
    branch = _require_env("TREEGIT_BRANCH")
    parent_branch = _require_env("TREEGIT_PARENT_BRANCH")
    agent_slot = _require_env("TREEGIT_AGENT_SLOT")
    agent_name = os.environ.get("TREEGIT_AGENT_NAME", f"agent{agent_slot}")
    repo_root = _require_env("TREEGIT_REPO_ROOT")

    template = args.prompt_file.read_text(encoding="utf-8")
    prompt = template.format(
        agent_slot=agent_slot,
        agent_name=agent_name,
        branch=branch,
        parent_branch=parent_branch,
        worktree=str(worktree),
        repo_root=repo_root,
    )

    command = [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "-C",
        str(worktree),
    ]
    if args.unsafe:
        command.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        command.extend(["--sandbox", "workspace-write"])
    if args.model:
        command.extend(["--model", args.model])
    if args.reasoning_effort:
        command.extend(["-c", f"model_reasoning_effort={json.dumps(args.reasoning_effort)}"])
    command.append(prompt)

    if args.tmux:
        exit_code = _run_in_tmux(
            command=command,
            worktree=worktree,
            agent_name=agent_name,
            session_name=_session_name(args.tmux_session_prefix),
        )
    else:
        completed = subprocess.run(command, text=True, check=False)
        exit_code = completed.returncode
    if exit_code == 0:
        _cleanup_paths(worktree, args.cleanup_path)
    return exit_code


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def _run_in_tmux(
    *,
    command: list[str],
    worktree: Path,
    agent_name: str,
    session_name: str,
) -> int:
    metadata_dir = worktree / ".treegit"
    metadata_dir.mkdir(parents=True, exist_ok=True)

    window_name = _window_name(agent_name)
    wait_token = f"{session_name}-{window_name}-done"
    log_path = metadata_dir / "codex_tmux.log"
    status_path = metadata_dir / "codex_tmux.exit"
    script_path = metadata_dir / "codex_tmux_runner.sh"
    metadata_path = metadata_dir / "codex_tmux.json"

    for path in (log_path, status_path, script_path, metadata_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    quoted_command = " ".join(shlex.quote(part) for part in command)
    script_path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -uo pipefail",
                f"cd {shlex.quote(str(worktree))}",
                f"{quoted_command} 2>&1 | tee -a {shlex.quote(str(log_path))}",
                'status=${PIPESTATUS[0]}',
                f"printf '%s\\n' \"$status\" > {shlex.quote(str(status_path))}",
                f"tmux wait-for -S {shlex.quote(wait_token)}",
                'exit "$status"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    script_path.chmod(0o755)

    metadata_path.write_text(
        json.dumps(
            {
                "session_name": session_name,
                "window_name": window_name,
                "worktree": str(worktree),
                "log_path": str(log_path),
                "status_path": str(status_path),
                "attach_command": f"tmux attach -t {session_name}",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    if not _tmux_has_session(session_name):
        started = subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                session_name,
                "-n",
                window_name,
                "-c",
                str(worktree),
                "bash",
                str(script_path),
            ],
            text=True,
            check=False,
            capture_output=True,
        )
        if started.returncode == 0:
            _tmux(["set-option", "-t", session_name, "remain-on-exit", "on"])
        elif not _tmux_has_session(session_name):
            raise SystemExit(started.stderr.strip() or started.stdout.strip() or "failed to start tmux session")

    existing_windows = _tmux_windows(session_name)
    if window_name in existing_windows:
        _tmux(
            [
                "respawn-window",
                "-k",
                "-t",
                f"{session_name}:{window_name}",
                "-c",
                str(worktree),
                "bash",
                str(script_path),
            ]
        )
    else:
        _tmux(
            [
                "new-window",
                "-d",
                "-t",
                session_name,
                "-n",
                window_name,
                "-c",
                str(worktree),
                "bash",
                str(script_path),
            ]
        )
        _tmux(["set-option", "-t", session_name, "remain-on-exit", "on"])

    waiter = subprocess.run(
        ["tmux", "wait-for", wait_token],
        text=True,
        check=False,
        capture_output=True,
    )
    if waiter.returncode != 0:
        raise SystemExit(waiter.stderr.strip() or waiter.stdout.strip() or "tmux wait-for failed")

    try:
        return int(status_path.read_text(encoding="utf-8").strip())
    except FileNotFoundError as exc:
        raise SystemExit(f"tmux session {session_name!r} finished without writing {status_path}") from exc
    except ValueError as exc:
        raise SystemExit(f"invalid exit code in {status_path}") from exc


def _session_name(session_prefix: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in session_prefix)
    return safe[:80]


def _window_name(agent_name: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in agent_name)
    return safe[:80]


def _tmux(args: list[str]) -> None:
    completed = subprocess.run(["tmux", *args], text=True, check=False, capture_output=True)
    if completed.returncode != 0:
        raise SystemExit(completed.stderr.strip() or completed.stdout.strip() or f"tmux command failed: {' '.join(args)}")


def _tmux_has_session(session_name: str) -> bool:
    completed = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def _tmux_windows(session_name: str) -> set[str]:
    completed = subprocess.run(
        ["tmux", "list-windows", "-t", session_name, "-F", "#{window_name}"],
        text=True,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise SystemExit(completed.stderr.strip() or completed.stdout.strip() or "failed to list tmux windows")
    return {line.strip() for line in completed.stdout.splitlines() if line.strip()}


def _cleanup_paths(worktree: Path, cleanup_paths: list[str]) -> None:
    for raw_path in cleanup_paths:
        candidate = (worktree / raw_path).resolve()
        try:
            candidate.relative_to(worktree)
        except ValueError as exc:
            raise SystemExit(f"cleanup path escapes worktree: {raw_path}") from exc
        if not candidate.exists() and not candidate.is_symlink():
            continue
        if candidate.is_dir() and not candidate.is_symlink():
            shutil.rmtree(candidate)
        else:
            candidate.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
