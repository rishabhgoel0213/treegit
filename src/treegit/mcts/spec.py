from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from treegit.errors import MCTSConfigError
from treegit.mcts.models import CommandSpec, ExpanderSpec, ObjectiveSpec, SearchSpec, SelectionSpec


def load_search_spec(path: Path, repo_root: Path) -> SearchSpec:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MCTSConfigError(f"config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise MCTSConfigError(f"invalid JSON in config file {path}: {exc}") from exc
    return _parse_search_spec(raw, repo_root=repo_root)


def search_spec_from_json(text: str) -> SearchSpec:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MCTSConfigError(f"invalid stored search config JSON: {exc}") from exc
    return _parse_search_spec(raw, repo_root=None)


def search_spec_to_json(spec: SearchSpec) -> str:
    payload = {
        "root_branch": spec.root_branch,
        "worktree_root": str(spec.worktree_root),
        "artifact_root": str(spec.artifact_root),
        "branch_prefix": spec.branch_prefix,
        "expansion_width": spec.expansion_width,
        "selection": {
            "policy": spec.selection.policy,
            "exploration_constant": spec.selection.exploration_constant,
        },
        "expander": {
            "command": list(spec.expander.command.command),
            "env": spec.expander.command.env,
            "cwd": spec.expander.command.cwd,
            "timeout_seconds": spec.expander.command.timeout_seconds,
            "commit_message_template": spec.expander.commit_message_template,
        },
        "objective": {
            "id": spec.objective.objective_id,
            "version": spec.objective.objective_version,
            "command": list(spec.objective.command.command),
            "env": spec.objective.command.env,
            "cwd": spec.objective.command.cwd,
            "timeout_seconds": spec.objective.command.timeout_seconds,
            "default_direction": spec.objective.default_direction,
            "failure_utility": spec.objective.failure_utility,
        },
    }
    return json.dumps(payload, sort_keys=True)


def _parse_search_spec(raw: Any, repo_root: Path | None) -> SearchSpec:
    if not isinstance(raw, dict):
        raise MCTSConfigError("config root must be a JSON object")
    root_branch = _require_string(raw, "root_branch", default="root")
    branch_prefix = _require_string(raw, "branch_prefix", default="mcts")
    expansion_width = _require_positive_int(raw, "expansion_width", default=1)
    worktree_value = _require_string(raw, "worktree_root")
    worktree_root = Path(worktree_value)
    if not worktree_root.is_absolute():
        if repo_root is None:
            raise MCTSConfigError("stored config has a relative worktree_root")
        worktree_root = (repo_root / worktree_root).resolve()
    else:
        worktree_root = worktree_root.resolve()
    artifact_value = _require_string(raw, "artifact_root", default=str(worktree_root.parent / "mcts-artifacts"))
    artifact_root = Path(artifact_value)
    if not artifact_root.is_absolute():
        if repo_root is None:
            raise MCTSConfigError("stored config has a relative artifact_root")
        artifact_root = (repo_root / artifact_root).resolve()
    else:
        artifact_root = artifact_root.resolve()

    selection_raw = _require_object(raw, "selection", default={})
    policy = _require_string(selection_raw, "policy", default="uct")
    if policy != "uct":
        raise MCTSConfigError(f"unsupported selection policy: {policy}")
    exploration_constant = _require_float(selection_raw, "exploration_constant", default=1.4)

    expander_raw = _require_object(raw, "expander")
    objective_raw = _require_object(raw, "objective")

    expander = ExpanderSpec(
        command=_parse_command_spec(expander_raw, "expander"),
        commit_message_template=_require_string(
            expander_raw,
            "commit_message_template",
            default="mcts expansion from {parent_branch} to {branch}",
        ),
    )

    default_direction = _require_string(objective_raw, "default_direction", default="maximize")
    if default_direction not in {"maximize", "minimize"}:
        raise MCTSConfigError(f"unsupported objective direction: {default_direction}")
    failure_utility = objective_raw.get("failure_utility")
    if failure_utility is not None:
        try:
            failure_utility = float(failure_utility)
        except (TypeError, ValueError) as exc:
            raise MCTSConfigError("objective.failure_utility must be numeric") from exc
    objective = ObjectiveSpec(
        objective_id=_require_string(objective_raw, "id"),
        objective_version=_require_string(objective_raw, "version", default="v1"),
        command=_parse_command_spec(objective_raw, "objective"),
        default_direction=default_direction,
        failure_utility=failure_utility,
    )

    return SearchSpec(
        root_branch=root_branch,
        worktree_root=worktree_root,
        artifact_root=artifact_root,
        branch_prefix=branch_prefix.strip("/"),
        expansion_width=expansion_width,
        selection=SelectionSpec(policy=policy, exploration_constant=exploration_constant),
        expander=expander,
        objective=objective,
    )


def _parse_command_spec(raw: dict[str, Any], label: str) -> CommandSpec:
    command = raw.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
        raise MCTSConfigError(f"{label}.command must be a non-empty list of strings")
    env_raw = raw.get("env", {})
    if not isinstance(env_raw, dict) or not all(isinstance(key, str) for key in env_raw):
        raise MCTSConfigError(f"{label}.env must be a string-to-string object")
    env: dict[str, str] = {}
    for key, value in env_raw.items():
        if not isinstance(value, str):
            raise MCTSConfigError(f"{label}.env[{key!r}] must be a string")
        env[key] = value
    cwd = raw.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise MCTSConfigError(f"{label}.cwd must be a string")
    timeout_seconds = raw.get("timeout_seconds")
    if timeout_seconds is not None:
        try:
            timeout_seconds = float(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise MCTSConfigError(f"{label}.timeout_seconds must be numeric") from exc
        if timeout_seconds <= 0:
            raise MCTSConfigError(f"{label}.timeout_seconds must be positive")
    return CommandSpec(
        command=tuple(command),
        env=env,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )


def _require_object(raw: dict[str, Any], key: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    value = raw.get(key, default)
    if not isinstance(value, dict):
        raise MCTSConfigError(f"{key} must be an object")
    return value


def _require_string(raw: dict[str, Any], key: str, default: str | None = None) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise MCTSConfigError(f"{key} must be a non-empty string")
    return value


def _require_positive_int(raw: dict[str, Any], key: str, default: int | None = None) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or value <= 0:
        raise MCTSConfigError(f"{key} must be a positive integer")
    return value


def _require_float(raw: dict[str, Any], key: str, default: float | None = None) -> float:
    value = raw.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise MCTSConfigError(f"{key} must be numeric") from exc
