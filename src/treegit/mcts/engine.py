from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import json
import math
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from treegit.errors import MCTSExecutionError, MCTSSelectionError, ReferenceResolutionError
from treegit.mcts.models import EvalResult, SearchEvalRecord, SearchNodeRecord, SelectionSpec, StepChildResult, StepResult
from treegit.mcts.runner import run_command
from treegit.mcts.spec import load_search_spec
from treegit.mcts.store import MCTSStore
from treegit.repository import Repository


MCTS_CONTEXT_DIR = ".treegit/mcts"
CHANGE_HISTORY_NAME = "change_history.md"
SCORE_HISTORY_NAME = "score_history.md"
CURRENT_CHANGE_NAME = "current_change.md"
NORMALIZED_UTILITY_CLIP = 3.0
NORMALIZED_UTILITY_EPS = 1e-6


@dataclass(frozen=True)
class _PreparedChild:
    parent_branch_name: str
    branch_name: str
    child_repo: Repository
    worktree_path: Path
    context: dict[str, str]


class MCTSEngine:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo
        self.store = MCTSStore(repo.index)
        self.store.init()

    def init_search(self, config_path: Path) -> None:
        spec = load_search_spec(config_path.resolve(), self.repo.root)
        if self.repo.index.get_branch(spec.root_branch) is None:
            raise ReferenceResolutionError(f"unknown root branch {spec.root_branch}")
        root_commit_id = self.repo.index.get_ref(spec.root_branch)
        next_child_index = self._next_child_index(spec.branch_prefix)
        self.store.create_search(spec.root_branch, root_commit_id, spec, next_child_index)

    def step(self) -> StepResult:
        search = self.store.get_search()
        selected_parents = self._select_nodes_for_budget(search)
        child_indices = self.store.reserve_child_indices(len(selected_parents))
        prepared_children: list[_PreparedChild] = []
        for agent_slot, (selected, child_index) in enumerate(zip(selected_parents, child_indices), start=1):
            child_branch = _branch_name(search.spec.branch_prefix, child_index)
            worktree_path = (search.spec.worktree_root / f"agent{agent_slot}").resolve()
            artifact_root = (search.spec.artifact_root / f"agent{agent_slot}" / child_branch.replace("/", "_")).resolve()
            self.repo.create_branch_from(
                name=child_branch,
                parent_name=selected.branch_name,
                commit_id=selected.commit_id,
            )
            self.store.add_child_node(
                branch_name=child_branch,
                parent_branch_name=selected.branch_name,
                depth=selected.depth + 1,
                worktree_path=str(worktree_path),
            )
            child_repo = self.repo.add_worktree(worktree_path, child_branch)
            context = {
                "repo_root": str(self.repo.root),
                "root_branch": search.root_branch,
                "branch": child_branch,
                "branch_leaf": child_branch.rsplit("/", 1)[-1],
                "branch_token": child_branch.replace("/", "_"),
                "parent_branch": selected.branch_name,
                "worktree": str(worktree_path),
                "artifact_root": str(artifact_root),
                "child_index": str(child_index),
                "agent_slot": str(agent_slot),
                "agent_name": f"agent{agent_slot}",
            }
            self._prepare_agent_context(selected, child_branch, worktree_path, context)
            prepared_children.append(
                _PreparedChild(
                    parent_branch_name=selected.branch_name,
                    branch_name=child_branch,
                    child_repo=child_repo,
                    worktree_path=worktree_path,
                    context=context,
                )
            )
        expander_results = self._run_expanders_parallel(search.spec, prepared_children)
        children = [
            self._finalize_child(search, selected, prepared_child, expander_results[prepared_child.branch_name])
            for selected, prepared_child in zip(selected_parents, prepared_children)
        ]

        frontier_count = self.store.frontier_count()
        best = self.store.best_node()
        next_status = "completed" if frontier_count == 0 else "running"
        self.store.increment_steps(status=next_status)
        search_after = self.store.get_search()
        return StepResult(
            selected_parents=[selected.branch_name for selected in selected_parents],
            children=children,
            frontier_count=frontier_count,
            best_branch=None if best is None else best.branch_name,
            steps_completed=search_after.steps_completed,
        )

    def run(self, steps: int) -> list[StepResult]:
        results: list[StepResult] = []
        for _ in range(steps):
            if self.store.frontier_count() == 0:
                self.store.set_search_status("completed")
                break
            results.append(self.step())
        return results

    def status(self) -> dict[str, object]:
        search = self.store.get_search()
        best = self.store.best_node()
        return {
            "status": search.status,
            "root_branch": search.root_branch,
            "steps_completed": search.steps_completed,
            "frontier_count": self.store.frontier_count(),
            "best_branch": None if best is None else best.branch_name,
            "best_utility": None if best is None else best.last_utility,
            "next_child_index": search.next_child_index,
        }

    def best(self) -> SearchNodeRecord | None:
        return self.store.best_node()

    def inspect(self, branch_name: str) -> tuple[SearchNodeRecord, SearchEvalRecord | None]:
        node = self.store.get_node(branch_name)
        if node is None:
            raise MCTSExecutionError(f"unknown MCTS node {branch_name!r}")
        if node.last_eval_id is None:
            return node, None
        return node, self.store.get_eval(node.last_eval_id)

    def _select_nodes_for_budget(self, search) -> list[SearchNodeRecord]:
        expandable = self.store.list_expandable_nodes()
        if not expandable:
            raise MCTSSelectionError("no expandable nodes remain")
        root = self.store.get_node(search.root_branch)
        root_visits = 0 if root is None else root.visit_count
        pending_by_branch: dict[str, int] = defaultdict(int)
        selected: list[SearchNodeRecord] = []
        for _ in range(search.spec.iteration_budget):
            total_visits = root_visits + len(selected)
            candidates: list[tuple[float, SearchNodeRecord, int]] = []
            for node in expandable:
                pending_expansions = pending_by_branch[node.branch_name]
                if not self._can_expand_node(node, pending_expansions, search.spec.selection):
                    continue
                score = self._score_expandable_node(
                    node,
                    pending_expansions=pending_expansions,
                    total_visits=total_visits,
                    selection=search.spec.selection,
                )
                candidates.append((score, node, pending_expansions))
            if not candidates:
                break
            candidates.sort(
                key=lambda item: (
                    -item[0],
                    float("inf") if item[1].last_utility is None else -item[1].last_utility,
                    item[1].child_count + item[2],
                    -item[1].depth,
                    item[1].branch_name,
                )
            )
            chosen = candidates[0][1]
            pending_by_branch[chosen.branch_name] += 1
            selected.append(chosen)
        if not selected:
            raise MCTSSelectionError("no nodes were eligible for expansion under the configured widening rule")
        return selected

    def _can_expand_node(self, node: SearchNodeRecord, pending_expansions: int, selection: SelectionSpec) -> bool:
        if node.status != "ready":
            return False
        projected_children = node.child_count + pending_expansions
        widening_trials = max(1, node.visit_count + projected_children + 1)
        max_children = max(
            1,
            math.ceil(selection.widening_coefficient * (widening_trials ** selection.widening_exponent)),
        )
        return projected_children < max_children

    def _score_expandable_node(
        self,
        node: SearchNodeRecord,
        *,
        pending_expansions: int,
        total_visits: int,
        selection: SelectionSpec,
    ) -> float:
        effective_visits = max(1, node.visit_count + pending_expansions)
        exploration = selection.exploration_constant * math.sqrt(math.log(total_visits + 1.0) / effective_visits)
        score = node.q_value + exploration
        if pending_expansions:
            score -= pending_expansions * selection.virtual_loss
        return score

    def _finalize_child(
        self,
        search,
        selected: SearchNodeRecord,
        prepared_child: _PreparedChild,
        expander_reason: str | None,
    ) -> StepChildResult:
        child_branch = prepared_child.branch_name
        child_repo = prepared_child.child_repo
        worktree_path = prepared_child.worktree_path
        context = prepared_child.context
        if expander_reason is not None:
            self.store.mark_node_status(child_branch, "failed", terminal_reason=expander_reason)
            return StepChildResult(
                parent_branch_name=prepared_child.parent_branch_name,
                branch_name=child_branch,
                status="failed",
                commit_id=None,
                utility=None,
                raw_score=None,
                reason=expander_reason,
                worktree_path=str(worktree_path),
            )

        status = child_repo.status()
        if not status.is_dirty():
            commit_id = child_repo.head_commit_id()
            self.store.update_node_commit(child_branch, commit_id)
            self.store.mark_node_status(child_branch, "terminal", terminal_reason="no_changes")
            return StepChildResult(
                parent_branch_name=prepared_child.parent_branch_name,
                branch_name=child_branch,
                status="terminal",
                commit_id=commit_id,
                utility=None,
                raw_score=None,
                reason="no_changes",
                worktree_path=str(worktree_path),
            )

        commit_message = search.spec.expander.commit_message_template.format(**context)
        commit_id = child_repo.commit(commit_message)
        self.store.update_node_commit(child_branch, commit_id)
        note_text = self._read_current_change_note(child_branch, selected.branch_name, worktree_path, context)
        if note_text is not None:
            self.store.upsert_note(child_branch, selected.branch_name, note_text)

        commit_record = child_repo.head_commit()
        state_id = None if commit_record is None else commit_record.root_tree_id
        if state_id is None:
            raise MCTSExecutionError(f"missing committed state for {child_branch}")
        cached = self.store.get_cached_eval(
            search.spec.objective.objective_id,
            search.spec.objective.objective_version,
            state_id,
        )
        if cached is None:
            result = self._run_objective(search.spec, context | {"commit_id": commit_id}, worktree_path)
        else:
            result = cached
        eval_id = str(uuid.uuid4())
        node_status = "ready" if result.utility is not None else "failed"
        terminal_reason = None if result.utility is not None else "objective_failed"
        normalized_utility = None if result.utility is None else self._normalize_utility(result.utility)
        self.store.record_eval(
            eval_id=eval_id,
            branch_name=child_branch,
            commit_id=commit_id,
            state_id=state_id,
            result=result,
            status=node_status,
            terminal_reason=terminal_reason,
        )
        self._refresh_agent_context(child_branch, worktree_path, context)
        if result.utility is not None:
            assert normalized_utility is not None
            self.store.backprop(child_branch, result.utility, normalized_utility)
        return StepChildResult(
            parent_branch_name=prepared_child.parent_branch_name,
            branch_name=child_branch,
            status=node_status,
            commit_id=commit_id,
            utility=result.utility,
            raw_score=result.raw_score,
            reason=terminal_reason,
            worktree_path=str(worktree_path),
        )

    def _run_expanders_parallel(self, spec, prepared_children: list[_PreparedChild]) -> dict[str, str | None]:
        if not prepared_children:
            return {}
        if len(prepared_children) == 1:
            prepared = prepared_children[0]
            return {
                prepared.branch_name: self._run_expander(
                    spec,
                    prepared.context,
                    prepared.worktree_path,
                )
            }
        max_workers = min(len(prepared_children), 32)
        branch_results: dict[str, str | None] = {}
        future_to_branch: dict[Future[str | None], str] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for prepared in prepared_children:
                future = executor.submit(
                    self._run_expander,
                    spec,
                    prepared.context,
                    prepared.worktree_path,
                )
                future_to_branch[future] = prepared.branch_name
            for future, branch_name in future_to_branch.items():
                branch_results[branch_name] = future.result()
        return branch_results

    def _run_expander(self, spec, context: dict[str, str], worktree_path: Path) -> str | None:
        try:
            completed = run_command(spec.expander.command, self._command_context(context), default_cwd=worktree_path)
        except MCTSExecutionError as exc:
            return str(exc)
        if completed.returncode == 0:
            return None
        return _truncate_reason(completed.stderr or completed.stdout or f"expander exit code {completed.returncode}")

    def _run_objective(self, spec, context: dict[str, str], worktree_path: Path) -> EvalResult:
        try:
            completed = run_command(spec.objective.command, self._command_context(context), default_cwd=worktree_path)
        except MCTSExecutionError as exc:
            return _failed_eval(spec, {"reason": str(exc)})
        if completed.returncode != 0:
            return _failed_eval(
                spec,
                {
                    "reason": f"objective exit code {completed.returncode}",
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                },
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return _failed_eval(
                spec,
                {
                    "reason": "objective output was not valid JSON",
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                },
            )
        if not isinstance(payload, dict):
            return _failed_eval(spec, {"reason": "objective output must be a JSON object"})
        direction = payload.get("direction", spec.objective.default_direction)
        if direction not in {"maximize", "minimize"}:
            return _failed_eval(spec, {"reason": f"invalid objective direction {direction!r}"})
        raw_score = payload.get("raw_score")
        if raw_score is not None:
            try:
                raw_score = float(raw_score)
            except (TypeError, ValueError):
                return _failed_eval(spec, {"reason": "objective raw_score must be numeric"})
        utility = payload.get("utility")
        if utility is None and raw_score is not None:
            utility = raw_score if direction == "maximize" else -raw_score
        elif utility is not None:
            try:
                utility = float(utility)
            except (TypeError, ValueError):
                return _failed_eval(spec, {"reason": "objective utility must be numeric"})
        success = bool(payload.get("success", utility is not None))
        if not success and spec.objective.failure_utility is not None and utility is None:
            utility = spec.objective.failure_utility
        return EvalResult(
            success=success,
            objective_id=str(payload.get("objective_id", spec.objective.objective_id)),
            objective_version=str(payload.get("objective_version", spec.objective.objective_version)),
            direction=direction,
            raw_score=raw_score,
            utility=utility,
            metrics=_coerce_dict(payload.get("metrics")),
            payload=payload,
            artifacts=_coerce_str_dict(payload.get("artifacts")),
        )

    def _command_context(self, context: dict[str, str]) -> dict[str, str]:
        rendered = dict(context)
        context_dir = (Path(context["worktree"]) / MCTS_CONTEXT_DIR).resolve()
        rendered.setdefault("TREEGIT_BRANCH", context["branch"])
        rendered.setdefault("TREEGIT_PARENT_BRANCH", context["parent_branch"])
        rendered.setdefault("TREEGIT_WORKTREE", context["worktree"])
        rendered.setdefault("TREEGIT_REPO_ROOT", context["repo_root"])
        rendered.setdefault("TREEGIT_AGENT_SLOT", context["agent_slot"])
        rendered.setdefault("TREEGIT_AGENT_NAME", context["agent_name"])
        rendered.setdefault("TREEGIT_ARTIFACT_ROOT", context["artifact_root"])
        rendered.setdefault("TREEGIT_CONTEXT_DIR", str(context_dir))
        rendered.setdefault("TREEGIT_CHANGE_HISTORY_FILE", str(context_dir / CHANGE_HISTORY_NAME))
        rendered.setdefault("TREEGIT_SCORE_HISTORY_FILE", str(context_dir / SCORE_HISTORY_NAME))
        rendered.setdefault("TREEGIT_CURRENT_CHANGE_FILE", str(context_dir / CURRENT_CHANGE_NAME))
        if "commit_id" in context:
            rendered.setdefault("TREEGIT_COMMIT_ID", context["commit_id"])
        return rendered

    def _prepare_agent_context(
        self,
        selected: SearchNodeRecord,
        child_branch: str,
        worktree_path: Path,
        context: dict[str, str],
    ) -> None:
        context_dir = (worktree_path / MCTS_CONTEXT_DIR).resolve()
        context_dir.mkdir(parents=True, exist_ok=True)
        (context_dir / CHANGE_HISTORY_NAME).write_text(
            self._render_change_history(selected.branch_name),
            encoding="utf-8",
        )
        (context_dir / SCORE_HISTORY_NAME).write_text(
            self._render_score_history(selected.branch_name),
            encoding="utf-8",
        )
        (context_dir / CURRENT_CHANGE_NAME).write_text(
            self._render_current_change_template(child_branch, selected.branch_name, context["agent_name"]),
            encoding="utf-8",
        )

    def _refresh_agent_context(self, branch_name: str, worktree_path: Path, context: dict[str, str]) -> None:
        context_dir = (worktree_path / MCTS_CONTEXT_DIR).resolve()
        context_dir.mkdir(parents=True, exist_ok=True)
        (context_dir / CHANGE_HISTORY_NAME).write_text(
            self._render_change_history(branch_name),
            encoding="utf-8",
        )
        (context_dir / SCORE_HISTORY_NAME).write_text(
            self._render_score_history(branch_name),
            encoding="utf-8",
        )
        (context_dir / CURRENT_CHANGE_NAME).write_text(
            self._render_current_change_template(branch_name, context["parent_branch"], context["agent_name"]),
            encoding="utf-8",
        )

    def _read_current_change_note(
        self,
        branch_name: str,
        parent_branch_name: str,
        worktree_path: Path,
        context: dict[str, str],
    ) -> str | None:
        note_path = (worktree_path / MCTS_CONTEXT_DIR / CURRENT_CHANGE_NAME).resolve()
        if not note_path.exists():
            return None
        note_text = note_path.read_text(encoding="utf-8").strip()
        template = self._render_current_change_template(branch_name, parent_branch_name, context["agent_name"]).strip()
        if not note_text or note_text == template:
            return None
        return note_text + "\n"

    def _render_change_history(self, branch_name: str) -> str:
        lines = [
            "# MCTS Change History",
            "",
            "Read this before editing. It contains the aggregated branch notes for the lineage leading to the current parent branch.",
            "",
        ]
        found = False
        for node in self.store.lineage(branch_name):
            note = self.store.get_note(node.branch_name)
            if note is None:
                continue
            found = True
            lines.append(note.note_text.rstrip())
            lines.append("")
        if not found:
            lines.extend(
                [
                    "No recorded branch notes exist yet for this lineage.",
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    def _render_score_history(self, branch_name: str) -> str:
        lines = [
            "# MCTS Score History",
            "",
            "This file summarizes the recorded evaluation outputs for the lineage leading to the current parent branch.",
            "",
        ]
        found = False
        for node in self.store.lineage(branch_name):
            if node.last_eval_id is None:
                continue
            evaluation = self.store.get_eval(node.last_eval_id)
            if evaluation is None:
                continue
            found = True
            metrics = evaluation.result.metrics
            lines.extend(
                [
                    f"## Branch: {node.branch_name}",
                    f"- status: {'success' if evaluation.result.success else 'failure'}",
                    f"- raw_score: {self._format_float(evaluation.result.raw_score)}",
                    f"- utility: {self._format_float(evaluation.result.utility)}",
                    f"- direction: {evaluation.result.direction}",
                ]
            )
            val_bpb = metrics.get("val_bpb")
            if val_bpb is not None:
                lines.append(f"- val_bpb: {val_bpb}")
            val_loss = metrics.get("val_loss")
            if val_loss is not None:
                lines.append(f"- val_loss: {val_loss}")
            reason = evaluation.result.payload.get("reason")
            if isinstance(reason, str) and reason.strip():
                lines.append(f"- reason: {reason.strip()}")
            lines.append("")
        if not found:
            lines.extend(
                [
                    "No evaluated branches exist yet for this lineage.",
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    def _render_current_change_template(self, branch_name: str, parent_branch_name: str, agent_name: str) -> str:
        return (
            "# Current Branch Change Note\n\n"
            "Fill in this file before you stop. The search harness will aggregate it into the lineage history for descendants.\n\n"
            f"Branch: {branch_name}\n"
            f"Parent: {parent_branch_name}\n"
            f"Agent: {agent_name}\n\n"
            "Summary:\n"
            "Hypothesis:\n"
            "Files Changed:\n"
            "- \n"
            "Validation:\n"
            "- not run\n"
            "Notes:\n"
            "- \n"
        )

    @staticmethod
    def _format_float(value: float | None) -> str:
        if value is None:
            return "n/a"
        return f"{value:.12g}"

    def _normalize_utility(self, utility: float) -> float:
        observed = self.store.list_backprop_utilities()
        sample = observed + [utility]
        center = median(sample)
        deviations = [abs(value - center) for value in sample]
        mad = median(deviations)
        scale = 1.4826 * mad
        if scale <= NORMALIZED_UTILITY_EPS:
            return 0.0
        normalized = (utility - center) / scale
        return max(-NORMALIZED_UTILITY_CLIP, min(NORMALIZED_UTILITY_CLIP, normalized))

    def _next_child_index(self, branch_prefix: str) -> int:
        pattern = re.compile(rf"^{re.escape(branch_prefix)}/(\d{{6}})$")
        highest = 0
        for branch in self.repo.list_branches():
            match = pattern.match(branch.name)
            if match is None:
                continue
            highest = max(highest, int(match.group(1)))
        return highest + 1


def _branch_name(branch_prefix: str, child_index: int) -> str:
    return f"{branch_prefix}/{child_index:06d}"


def _coerce_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _coerce_str_dict(value) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        if isinstance(key, str) and isinstance(item, str):
            result[key] = item
    return result


def _failed_eval(spec, payload: dict[str, object]) -> EvalResult:
    utility = spec.objective.failure_utility
    return EvalResult(
        success=False,
        objective_id=spec.objective.objective_id,
        objective_version=spec.objective.objective_version,
        direction=spec.objective.default_direction,
        raw_score=None,
        utility=utility,
        metrics={},
        payload=payload,
        artifacts={},
    )


def _truncate_reason(text: str, limit: int = 240) -> str:
    clean = " ".join(text.split())
    return clean if len(clean) <= limit else f"{clean[: limit - 3]}..."
