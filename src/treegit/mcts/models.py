from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CommandSpec:
    command: tuple[str, ...]
    env: dict[str, str]
    cwd: str | None
    timeout_seconds: float | None


@dataclass(frozen=True)
class SelectionSpec:
    policy: str
    exploration_constant: float
    widening_coefficient: float
    widening_exponent: float
    virtual_loss: float


@dataclass(frozen=True)
class ExpanderSpec:
    command: CommandSpec
    commit_message_template: str


@dataclass(frozen=True)
class ObjectiveSpec:
    objective_id: str
    objective_version: str
    command: CommandSpec
    default_direction: str
    failure_utility: float | None


@dataclass(frozen=True)
class SearchSpec:
    root_branch: str
    worktree_root: Path
    artifact_root: Path
    branch_prefix: str
    iteration_budget: int
    selection: SelectionSpec
    expander: ExpanderSpec
    objective: ObjectiveSpec

    @property
    def expansion_width(self) -> int:
        return self.iteration_budget


@dataclass(frozen=True)
class SearchRunRecord:
    root_branch: str
    root_commit_id: str | None
    spec: SearchSpec
    next_child_index: int
    status: str
    steps_completed: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SearchNodeRecord:
    branch_name: str
    parent_branch_name: str | None
    commit_id: str | None
    depth: int
    child_count: int
    visit_count: int
    value_sum: float
    normalized_value_sum: float
    last_utility: float | None
    last_raw_score: float | None
    last_eval_id: str | None
    status: str
    terminal_reason: str | None
    worktree_path: str | None
    created_at: str
    updated_at: str
    parent_visit_count: int | None = None

    @property
    def q_value(self) -> float:
        if self.visit_count <= 0:
            return 0.0
        return self.normalized_value_sum / self.visit_count


@dataclass(frozen=True)
class EvalResult:
    success: bool
    objective_id: str
    objective_version: str
    direction: str
    raw_score: float | None
    utility: float | None
    metrics: dict[str, Any]
    payload: dict[str, Any]
    artifacts: dict[str, str]


@dataclass(frozen=True)
class SearchEvalRecord:
    eval_id: str
    branch_name: str
    commit_id: str
    state_id: str
    result: EvalResult
    created_at: str


@dataclass(frozen=True)
class SearchNoteRecord:
    branch_name: str
    parent_branch_name: str | None
    note_text: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class StepChildResult:
    parent_branch_name: str
    branch_name: str
    status: str
    commit_id: str | None
    utility: float | None
    raw_score: float | None
    reason: str | None
    worktree_path: str


@dataclass(frozen=True)
class StepResult:
    selected_parents: list[str]
    children: list[StepChildResult]
    frontier_count: int
    best_branch: str | None
    steps_completed: int

    @property
    def selected_branch(self) -> str:
        if not self.selected_parents:
            return ""
        ordered_unique: list[str] = []
        seen: set[str] = set()
        for branch_name in self.selected_parents:
            if branch_name in seen:
                continue
            seen.add(branch_name)
            ordered_unique.append(branch_name)
        if len(ordered_unique) == 1:
            return ordered_unique[0]
        return ", ".join(ordered_unique)
