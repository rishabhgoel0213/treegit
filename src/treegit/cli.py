from __future__ import annotations

import argparse
from collections import defaultdict
from html import escape
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time

from treegit.errors import MCTSRunNotFoundError, TreeGitError
from treegit.mcts import MCTSEngine
from treegit.repository import Repository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="treegit")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init")
    subparsers.add_parser("reset")
    subparsers.add_parser("status")

    commit_parser = subparsers.add_parser("commit")
    commit_parser.add_argument("-m", "--message", required=True)

    log_parser = subparsers.add_parser("log")
    log_parser.add_argument("revision", nargs="?")

    branch_parser = subparsers.add_parser("branch")
    branch_parser.add_argument("name", nargs="?")

    worktree_parser = subparsers.add_parser("worktree")
    worktree_subparsers = worktree_parser.add_subparsers(dest="worktree_command", required=True)
    worktree_add_parser = worktree_subparsers.add_parser("add")
    worktree_add_parser.add_argument("path")
    worktree_add_parser.add_argument("branch")

    checkout_parser = subparsers.add_parser("checkout")
    checkout_parser.add_argument("revision")
    checkout_parser.add_argument("--force", action="store_true")

    diff_parser = subparsers.add_parser("diff")
    diff_parser.add_argument("left", nargs="?")
    diff_parser.add_argument("right", nargs="?")

    search_parser = subparsers.add_parser("search")
    search_parser.add_argument("query")
    search_parser.add_argument("--field", choices=["content", "path", "commit", "branch", "all"], default="all")
    search_parser.add_argument("--branch")
    search_parser.add_argument("--path")
    search_parser.add_argument("--limit", type=int, default=20)

    metric_parser = subparsers.add_parser("metric")
    metric_subparsers = metric_parser.add_subparsers(dest="metric_command", required=True)

    metric_define_parser = metric_subparsers.add_parser("define")
    metric_define_parser.add_argument("name")

    metric_get_parser = metric_subparsers.add_parser("get")
    metric_get_parser.add_argument("name")

    metric_backprop_parser = metric_subparsers.add_parser("backprop")
    metric_backprop_parser.add_argument("name")
    metric_backprop_parser.add_argument("value", type=float)

    mcts_parser = subparsers.add_parser("mcts")
    mcts_subparsers = mcts_parser.add_subparsers(dest="mcts_command", required=True)

    mcts_init_parser = mcts_subparsers.add_parser("init")
    mcts_init_parser.add_argument("--config", required=True)

    mcts_subparsers.add_parser("step")

    mcts_run_parser = mcts_subparsers.add_parser("run")
    mcts_run_parser.add_argument("--steps", type=int, default=1)
    mcts_run_parser.add_argument("--background", action="store_true")
    mcts_run_parser.add_argument("--log-file")
    mcts_run_parser.add_argument("--background-child", action="store_true", help=argparse.SUPPRESS)
    mcts_run_parser.add_argument("--background-state-file", help=argparse.SUPPRESS)

    mcts_subparsers.add_parser("status")

    mcts_subparsers.add_parser("best")

    mcts_subparsers.add_parser("stop")

    mcts_inspect_parser = mcts_subparsers.add_parser("inspect")
    mcts_inspect_parser.add_argument("branch")

    mcts_plot_parser = mcts_subparsers.add_parser("plot")
    mcts_plot_parser.add_argument("--output")
    mcts_plot_parser.add_argument("--var", default="score")
    mcts_plot_parser.add_argument("--branch")
    mcts_plot_parser.add_argument("--view", choices=("lineage", "tree"), default="lineage")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run_command(args)
    except TreeGitError as exc:
        parser.exit(status=1, message=f"error: {exc}\n")


def run_command(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    if args.command == "init":
        repo = Repository.init(cwd)
        print(f"Initialized empty TreeGit repository in {repo.git_dir}")
        return 0
    repo = Repository.discover(cwd)
    primary_repo = repo.primary_repository()
    if args.command == "reset":
        if repo.root.resolve() != primary_repo.root.resolve():
            raise TreeGitError("reset must be run from the primary repository")
        stopped = _stop_active_mcts(primary_repo)
        engine = MCTSEngine(primary_repo)
        engine.store.reset_search()
        worktree_paths = _linked_worktree_paths(primary_repo, engine)
        primary_repo.delete_all_non_root_branches()
        for worktree_path in worktree_paths:
            if worktree_path == primary_repo.root.resolve():
                continue
            if worktree_path.exists():
                shutil.rmtree(worktree_path)
            primary_repo.unregister_worktree(worktree_path)
        print("reset complete")
        print(f"worktrees_removed: {len(worktree_paths)}")
        if stopped["background_pid"] is not None:
            print(f"background_pid: {stopped['background_pid']}")
        if stopped["tmux_session"] is not None:
            print(f"tmux_session: {stopped['tmux_session']}")
        return 0
    if args.command == "status":
        report = repo.status()
        if not report.is_dirty():
            print("clean")
            return 0
        for label, paths in [
            ("added", report.added),
            ("modified", report.modified),
            ("deleted", report.deleted),
            ("untracked", report.untracked),
        ]:
            if not paths:
                continue
            print(f"{label}:")
            for path in paths:
                print(f"  {path}")
        return 0
    if args.command == "commit":
        commit_id = repo.commit(args.message)
        print(commit_id)
        return 0
    if args.command == "log":
        for commit in repo.log(args.revision):
            print(commit.commit_id)
            print(f"Date: {commit.created_at}")
            print(f"    {commit.message}")
            print()
        return 0
    if args.command == "branch":
        if args.name:
            repo.create_branch(args.name)
            return 0
        head = repo.current_branch()
        branches = repo.list_branches()
        children: dict[str | None, list] = defaultdict(list)
        for branch in branches:
            children[branch.parent_name].append(branch)
        for child_list in children.values():
            child_list.sort(key=lambda item: item.name)

        def render(parent_name: str | None, depth: int) -> None:
            for branch in children.get(parent_name, []):
                marker = "*" if branch.name == head else " "
                suffix = f" {branch.commit_id[:12]}" if branch.commit_id else ""
                indent = "  " * depth
                print(f"{marker} {indent}{branch.name}{suffix}")
                render(branch.name, depth + 1)

        render(None, 0)
        return 0
    if args.command == "worktree":
        if args.worktree_command == "add":
            worktree = repo.add_worktree(Path(args.path), args.branch)
            print(f"Created worktree for {args.branch} at {worktree.root}")
            return 0
    if args.command == "checkout":
        commit_id = repo.checkout(args.revision, force=args.force)
        print(commit_id)
        return 0
    if args.command == "diff":
        print(repo.diff(args.left, args.right), end="")
        return 0
    if args.command == "search":
        results = repo.search(
            args.query,
            field=args.field,
            branch=args.branch,
            path_glob=args.path,
            limit=args.limit,
        )
        for category in ["branch", "commit", "path", "content"]:
            items = results.get(category)
            if not items:
                continue
            print(f"{category}:")
            for item in items:
                details = item.summary if item.path is None else f"{item.path}: {item.summary}"
                suffix = f" ({item.created_at})" if item.created_at else ""
                print(f"  {item.ref} {details}{suffix}")
        return 0
    if args.command == "metric":
        if args.metric_command == "define":
            repo.define_metric(args.name)
            return 0
        if args.metric_command == "get":
            print(repo.get_metric(args.name))
            return 0
        if args.metric_command == "backprop":
            repo.backprop_metric(args.name, args.value)
            return 0
    if args.command == "mcts":
        engine = MCTSEngine(primary_repo)
        if args.mcts_command == "init":
            engine.init_search(Path(args.config))
            print("initialized")
            return 0
        if args.mcts_command == "step":
            result = engine.step()
            print(f"selected: {_format_selected_parents(result.selected_parents)}")
            for child in result.children:
                utility = "None" if child.utility is None else f"{child.utility:.6f}"
                print(
                    f"{child.branch_name} parent={child.parent_branch_name} status={child.status} commit={child.commit_id or ''} "
                    f"utility={utility} reason={child.reason or ''}".rstrip()
                )
            print(f"frontier_count: {result.frontier_count}")
            if result.best_branch is not None:
                print(f"best_branch: {result.best_branch}")
            return 0
        if args.mcts_command == "run":
            if args.background and not args.background_child:
                log_path, pid = _spawn_background_mcts_run(primary_repo, steps=args.steps, log_file=args.log_file)
                print(f"background_pid: {pid}")
                print(f"log_file: {log_path}")
                return 0
            if args.background_child:
                _write_background_state(
                    primary_repo,
                    pid=os.getpid(),
                    log_path=_resolve_log_path(primary_repo, args.log_file),
                    tmux_session_name=_configured_tmux_session_name(engine),
                )
            try:
                results = _run_mcts_steps(engine, args.steps)
            finally:
                if args.background_child:
                    _clear_background_state(primary_repo, expected_pid=os.getpid())
            print(f"steps_executed: {len(results)}", flush=True)
            if results:
                print(f"last_selected: {_format_selected_parents(results[-1].selected_parents)}", flush=True)
                if results[-1].best_branch is not None:
                    print(f"best_branch: {results[-1].best_branch}", flush=True)
            return 0
        if args.mcts_command == "stop":
            stopped = _stop_active_mcts(primary_repo)
            if stopped["background_pid"] is None and stopped["tmux_session"] is None:
                print("idle")
            else:
                if stopped["background_pid"] is not None:
                    print(f"background_pid: {stopped['background_pid']}")
                if stopped["tmux_session"] is not None:
                    print(f"tmux_session: {stopped['tmux_session']}")
            return 0
        if args.mcts_command == "status":
            status = engine.status()
            for key in ["status", "root_branch", "steps_completed", "frontier_count", "best_branch", "best_utility", "next_child_index"]:
                print(f"{key}: {status[key]}")
            return 0
        if args.mcts_command == "best":
            best = engine.best()
            if best is None:
                print("none")
            else:
                print(best.branch_name)
                print(f"utility: {best.last_utility}")
                print(f"raw_score: {best.last_raw_score}")
                print(f"visits: {best.visit_count}")
                print(f"value_sum: {best.value_sum}")
            return 0
        if args.mcts_command == "inspect":
            node, evaluation = engine.inspect(args.branch)
            print(f"branch: {node.branch_name}")
            print(f"status: {node.status}")
            print(f"parent_branch: {node.parent_branch_name}")
            print(f"commit_id: {node.commit_id}")
            print(f"depth: {node.depth}")
            print(f"child_count: {node.child_count}")
            print(f"visits: {node.visit_count}")
            print(f"value_sum: {node.value_sum}")
            print(f"q_value: {node.q_value}")
            print(f"last_utility: {node.last_utility}")
            print(f"last_raw_score: {node.last_raw_score}")
            print(f"last_eval_id: {node.last_eval_id}")
            if evaluation is None:
                print("evaluation: none")
                return 0
            print(f"objective_id: {evaluation.result.objective_id}")
            print(f"objective_version: {evaluation.result.objective_version}")
            print(f"direction: {evaluation.result.direction}")
            print(f"eval_success: {evaluation.result.success}")
            print(f"eval_created_at: {evaluation.created_at}")
            for key in sorted(evaluation.result.metrics):
                print(f"metric.{key}: {evaluation.result.metrics[key]}")
            for key in sorted(evaluation.result.artifacts):
                print(f"artifact.{key}: {evaluation.result.artifacts[key]}")
            return 0
        if args.mcts_command == "plot":
            output_path, plotted_branch, point_count = _write_mcts_plot(
                primary_repo,
                engine,
                args.output,
                args.var,
                args.branch,
                args.view,
            )
            print(f"output: {output_path}")
            print(f"branch: {plotted_branch}")
            print(f"var: {args.var}")
            print(f"view: {args.view}")
            print(f"points: {point_count}")
            return 0
    return 0


def _run_mcts_steps(engine: MCTSEngine, steps: int) -> list:
    results = []
    for index in range(steps):
        if engine.store.frontier_count() == 0:
            engine.store.set_search_status("completed")
            break
        result = engine.step()
        results.append(result)
        print(f"step: {index + 1}", flush=True)
        print(f"selected: {_format_selected_parents(result.selected_parents)}", flush=True)
        for child in result.children:
            utility = "None" if child.utility is None else f"{child.utility:.6f}"
            print(
                f"{child.branch_name} parent={child.parent_branch_name} status={child.status} commit={child.commit_id or ''} "
                f"utility={utility} reason={child.reason or ''}".rstrip(),
                flush=True,
            )
        print(f"frontier_count: {result.frontier_count}", flush=True)
        if result.best_branch is not None:
            print(f"best_branch: {result.best_branch}", flush=True)
    return results


def _format_selected_parents(selected_parents: list[str]) -> str:
    if not selected_parents:
        return "none"
    counts: dict[str, int] = {}
    for branch_name in selected_parents:
        counts[branch_name] = counts.get(branch_name, 0) + 1
    parts = []
    for branch_name, count in counts.items():
        parts.append(branch_name if count == 1 else f"{branch_name} x{count}")
    return ", ".join(parts)


def _spawn_background_mcts_run(repo: Repository, *, steps: int, log_file: str | None) -> tuple[Path, int]:
    log_path = _resolve_log_path(repo, log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    state_path = _background_state_path(repo)
    command = [
        sys.executable,
        "-m",
        "treegit",
        "mcts",
        "run",
        "--steps",
        str(steps),
        "--background-child",
        "--background-state-file",
        str(state_path),
    ]
    if log_file is not None:
        command.extend(["--log-file", str(log_path)])
    env = dict(os.environ)
    existing_pythonpath = env.get("PYTHONPATH")
    source_root = str(Path(__file__).resolve().parents[1])
    if existing_pythonpath:
        parts = existing_pythonpath.split(os.pathsep)
        if source_root not in parts:
            env["PYTHONPATH"] = os.pathsep.join([source_root, *parts])
    else:
        env["PYTHONPATH"] = source_root
    env["PYTHONUNBUFFERED"] = "1"
    with open(log_path, "a", encoding="utf-8", buffering=1) as handle:
        handle.write(f"[treegit] background run started {time.strftime('%Y-%m-%d %H:%M:%S %z')}\n")
        handle.flush()
        process = subprocess.Popen(
            command,
            cwd=repo.root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    _write_background_state(
        repo,
        pid=process.pid,
        log_path=log_path,
        tmux_session_name=_configured_tmux_session_name(MCTSEngine(repo)),
    )
    return log_path, process.pid


def _resolve_log_path(repo: Repository, raw_path: str | None) -> Path:
    if raw_path is None:
        return (repo.git_dir / "mcts-run.log").resolve()
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (repo.root / path).resolve()
    return path


def _background_state_path(repo: Repository) -> Path:
    return (repo.git_dir / "mcts-background.json").resolve()


def _write_background_state(
    repo: Repository,
    *,
    pid: int,
    log_path: Path,
    tmux_session_name: str | None,
) -> None:
    state_path = _background_state_path(repo)
    payload = {
        "pid": pid,
        "log_file": str(log_path),
        "tmux_session": tmux_session_name,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    state_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_background_state(repo: Repository) -> dict[str, object] | None:
    state_path = _background_state_path(repo)
    if not state_path.exists():
        return None
    return json.loads(state_path.read_text(encoding="utf-8"))


def _clear_background_state(repo: Repository, expected_pid: int | None = None) -> None:
    state_path = _background_state_path(repo)
    if not state_path.exists():
        return
    if expected_pid is not None:
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state_path.unlink(missing_ok=True)
            return
        if int(payload.get("pid", -1)) != expected_pid:
            return
    state_path.unlink(missing_ok=True)


def _configured_tmux_session_name(engine: MCTSEngine) -> str | None:
    try:
        search = engine.store.get_search()
    except MCTSRunNotFoundError:
        return None
    command = list(search.spec.expander.command.command)
    if "--tmux" not in command:
        return None
    prefix = "treegit-codex"
    if "--tmux-session-prefix" in command:
        index = command.index("--tmux-session-prefix")
        if index + 1 < len(command):
            prefix = command[index + 1]
    return _sanitize_tmux_name(prefix)


def _sanitize_tmux_name(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)
    return safe[:80]


def _stop_active_mcts(repo: Repository) -> dict[str, int | str | None]:
    background_pid: int | None = None
    tmux_session: str | None = None
    state = _read_background_state(repo)
    if state is not None:
        raw_pid = state.get("pid")
        if isinstance(raw_pid, int):
            background_pid = raw_pid
            _terminate_process_group(background_pid)
        raw_tmux = state.get("tmux_session")
        if isinstance(raw_tmux, str) and raw_tmux.strip():
            tmux_session = raw_tmux
        _clear_background_state(repo)

    engine = MCTSEngine(repo)
    configured_session = _configured_tmux_session_name(engine)
    if tmux_session is None:
        tmux_session = configured_session
    if tmux_session is not None:
        _kill_tmux_session(tmux_session)
    try:
        engine.store.set_search_status("stopped")
    except MCTSRunNotFoundError:
        pass
    return {
        "background_pid": background_pid,
        "tmux_session": tmux_session,
    }


def _terminate_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_tmux_session(session_name: str) -> None:
    completed = subprocess.run(
        ["tmux", "kill-session", "-t", session_name],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode not in {0, 1}:
        raise TreeGitError(f"failed to kill tmux session {session_name!r}")


def _linked_worktree_paths(repo: Repository, engine: MCTSEngine) -> list[Path]:
    paths: set[Path] = set()
    for worktree_path in repo.registered_worktrees():
        paths.add(worktree_path)
    for raw_path in engine.store.list_worktree_paths():
        paths.add(Path(raw_path).resolve())
    search_root = repo.root.parent.resolve()
    common_dir = repo.common_dir.resolve()
    for commondir_file in search_root.rglob("commondir"):
        if commondir_file.parent.name != ".treegit":
            continue
        try:
            raw = commondir_file.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not raw:
            continue
        candidate_common = Path(raw)
        if not candidate_common.is_absolute():
            candidate_common = (commondir_file.parent / candidate_common).resolve()
        else:
            candidate_common = candidate_common.resolve()
        if candidate_common != common_dir:
            continue
        paths.add(commondir_file.parent.parent.resolve())
    return sorted(path for path in paths if path != repo.root.resolve())


def _write_mcts_plot(
    repo: Repository,
    engine: MCTSEngine,
    raw_output_path: str | None,
    variable: str,
    branch_name: str | None,
    view: str,
) -> tuple[Path, str, int]:
    if view == "tree":
        return _write_tree_plot(repo, engine, raw_output_path, variable, branch_name)
    return _write_lineage_plot(repo, engine, raw_output_path, variable, branch_name)


def _write_lineage_plot(
    repo: Repository,
    engine: MCTSEngine,
    raw_output_path: str | None,
    variable: str,
    branch_name: str | None,
) -> tuple[Path, str, int]:
    plotted_branch = branch_name
    if plotted_branch is None:
        best = engine.best()
        if best is None or best.last_raw_score is None:
            raise TreeGitError("no evaluated MCTS branches to plot")
        plotted_branch = best.branch_name
    lineage = engine.store.lineage(plotted_branch)
    points = []
    missing = []
    for node in lineage:
        if node.last_eval_id is None:
            continue
        evaluation = engine.store.get_eval(node.last_eval_id)
        if evaluation is None:
            continue
        value = _extract_plot_value(node, evaluation, variable)
        if value is None:
            missing.append(node.branch_name)
            continue
        points.append(
            {
                "branch_name": node.branch_name,
                "depth": node.depth,
                "value": value,
                "created_at": evaluation.created_at,
            }
        )
    if not points:
        available = []
        target_node = engine.store.get_node(plotted_branch)
        target_eval = None if target_node is None or target_node.last_eval_id is None else engine.store.get_eval(target_node.last_eval_id)
        if target_eval is not None:
            available.extend(sorted(target_eval.result.metrics))
            payload_inputs = target_eval.result.payload.get("inputs")
            if isinstance(payload_inputs, dict):
                available.extend(sorted(str(key) for key in payload_inputs))
        available.extend(["score", "raw_score", "utility", "q_value"])
        available = sorted(set(available))
        raise TreeGitError(
            f"no values found for variable {variable!r}; available examples: {', '.join(available[:12])}"
        )
    output_path = _resolve_plot_path(
        repo,
        raw_output_path,
        variable,
        plotted_branch,
        branch_name is None,
        view="lineage",
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_render_lineage_svg(points, variable, plotted_branch, branch_name is None), encoding="utf-8")
    return output_path, plotted_branch, len(points)


def _write_tree_plot(
    repo: Repository,
    engine: MCTSEngine,
    raw_output_path: str | None,
    variable: str,
    branch_name: str | None,
) -> tuple[Path, str, int]:
    nodes = engine.store.list_nodes()
    if not nodes:
        raise TreeGitError("no MCTS nodes to plot")
    nodes_by_branch = {node.branch_name: node for node in nodes}
    focus_branch = branch_name
    if focus_branch is None:
        best = engine.best()
        focus_branch = best.branch_name if best is not None else nodes[0].branch_name
    if focus_branch not in nodes_by_branch:
        raise TreeGitError(f"unknown MCTS node {focus_branch!r}")

    points = []
    has_values = False
    for node in nodes:
        evaluation = None if node.last_eval_id is None else engine.store.get_eval(node.last_eval_id)
        value = None if evaluation is None else _extract_plot_value(node, evaluation, variable)
        if value is not None:
            has_values = True
        points.append(
            {
                "branch_name": node.branch_name,
                "parent_branch_name": node.parent_branch_name,
                "depth": node.depth,
                "status": node.status,
                "child_count": node.child_count,
                "visit_count": node.visit_count,
                "value": value,
            }
        )
    if not has_values:
        raise TreeGitError(
            f"no values found for variable {variable!r}; available examples: {', '.join(_available_plot_variables(engine, focus_branch)[:12])}"
        )
    output_path = _resolve_plot_path(
        repo,
        raw_output_path,
        variable,
        focus_branch,
        branch_name is None,
        view="tree",
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_render_tree_svg(points, variable, focus_branch, branch_name is None), encoding="utf-8")
    return output_path, focus_branch, len(points)


def _resolve_plot_path(
    repo: Repository,
    raw_path: str | None,
    variable: str,
    branch_name: str,
    is_best_default: bool,
    view: str,
) -> Path:
    if raw_path is None:
        if view == "tree":
            if is_best_default:
                if variable in {"score", "raw_score"}:
                    filename = "mcts-tree.svg"
                else:
                    filename = f"mcts-tree-{_sanitize_plot_var(variable)}.svg"
            else:
                branch_slug = _sanitize_plot_var(branch_name.replace("/", "-"))
                if variable in {"score", "raw_score"}:
                    filename = f"mcts-tree-{branch_slug}.svg"
                else:
                    filename = f"mcts-tree-{branch_slug}-{_sanitize_plot_var(variable)}.svg"
        else:
            if is_best_default:
                if variable in {"score", "raw_score"}:
                    filename = "mcts-best-path.svg"
                else:
                    filename = f"mcts-best-path-{_sanitize_plot_var(variable)}.svg"
            else:
                branch_slug = _sanitize_plot_var(branch_name.replace("/", "-"))
                if variable in {"score", "raw_score"}:
                    filename = f"mcts-lineage-{branch_slug}.svg"
                else:
                    filename = f"mcts-lineage-{branch_slug}-{_sanitize_plot_var(variable)}.svg"
        return (repo.git_dir / filename).resolve()
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (repo.root / path).resolve()
    return path


def _sanitize_plot_var(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)
    return safe.strip("-_") or "value"


def _extract_plot_value(node, evaluation, variable: str) -> float | None:
    aliases = {
        "score": node.last_raw_score,
        "raw_score": node.last_raw_score,
        "utility": node.last_utility,
        "q_value": node.q_value,
    }
    if variable in aliases and aliases[variable] is not None:
        return float(aliases[variable])

    for source in (
        evaluation.result.metrics,
        evaluation.result.payload,
        evaluation.result.payload.get("inputs") if isinstance(evaluation.result.payload, dict) else None,
    ):
        if not isinstance(source, dict):
            continue
        if variable not in source:
            continue
        value = source[variable]
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _available_plot_variables(engine: MCTSEngine, branch_name: str) -> list[str]:
    available = []
    target_node = engine.store.get_node(branch_name)
    target_eval = None if target_node is None or target_node.last_eval_id is None else engine.store.get_eval(target_node.last_eval_id)
    if target_eval is not None:
        available.extend(sorted(target_eval.result.metrics))
        payload_inputs = target_eval.result.payload.get("inputs")
        if isinstance(payload_inputs, dict):
            available.extend(sorted(str(key) for key in payload_inputs))
    available.extend(["score", "raw_score", "utility", "q_value"])
    return sorted(set(available))


def _render_lineage_svg(points, variable: str, branch_name: str, is_best_default: bool) -> str:
    point_count = len(points)
    width = min(1800, max(960, 220 + point_count * 22))
    height = 620
    margin_left = 110
    margin_right = 36
    margin_top = 78
    margin_bottom = 100
    plot_width = width - margin_left - margin_right
    plot_height = height - margin_top - margin_bottom

    values = [float(point["value"]) for point in points]
    min_value = min(values)
    max_value = max(values)
    if min_value == max_value:
        min_value -= 1.0
        max_value += 1.0
    value_span = max_value - min_value
    if point_count == 1:
        x_positions = [margin_left + plot_width / 2.0]
    else:
        x_step = plot_width / float(point_count - 1)
        x_positions = [margin_left + i * x_step for i in range(point_count)]

    def y_for_value(value: float) -> float:
        normalized = (value - min_value) / value_span
        return margin_top + (1.0 - normalized) * plot_height

    chart_points = [(x, y_for_value(float(point["value"]))) for x, point in zip(x_positions, points)]
    polyline_points = " ".join(f"{x:.2f},{y:.2f}" for x, y in chart_points)

    grid_lines = []
    tick_labels = []
    tick_count = 6
    for index in range(tick_count + 1):
        ratio = index / tick_count
        value = max_value - ratio * value_span
        y = margin_top + ratio * plot_height
        grid_lines.append(
            f'<line x1="{margin_left}" y1="{y:.2f}" x2="{width - margin_right}" y2="{y:.2f}" '
            'stroke="#e5e7eb" stroke-width="1" />'
        )
        tick_labels.append(
            f'<text x="{margin_left - 12}" y="{y + 4:.2f}" text-anchor="end" '
            'font-family="monospace" font-size="12" fill="#374151">'
            f"{escape(_format_plot_value(value))}"
            "</text>"
        )

    x_tick_labels = []
    label_target = min(8, point_count)
    label_step = max(1, (point_count - 1) // max(1, label_target - 1))
    label_indices = sorted(set([0, point_count - 1, *range(0, point_count, label_step)]))
    for index in label_indices:
        x = x_positions[index]
        x_tick_labels.append(
            f'<line x1="{x:.2f}" y1="{height - margin_bottom}" x2="{x:.2f}" y2="{height - margin_bottom + 6}" '
            'stroke="#6b7280" stroke-width="1" />'
        )
        x_tick_labels.append(
            f'<text x="{x:.2f}" y="{height - margin_bottom + 22:.2f}" text-anchor="middle" '
            'font-family="monospace" font-size="11" fill="#374151">'
            f"{index + 1}"
            "</text>"
        )

    point_elements = []
    for index, ((x, y), point) in enumerate(zip(chart_points, points)):
        value = float(point["value"])
        radius = 6.0 if index in {0, point_count - 1} else 4.0
        fill = "#0f766e" if index == point_count - 1 else "#2563eb"
        point_elements.append(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{radius:.1f}" fill="{fill}" stroke="white" stroke-width="1.5">'
            f"<title>{escape(point['branch_name'])} | {escape(variable)}={escape(_format_plot_value(value))}</title>"
            "</circle>"
        )

    start = float(points[0]["value"])
    end = float(points[-1]["value"])
    higher_is_better = variable in {"utility", "q_value"}
    improvement = end - start if higher_is_better else start - end
    direction_text = "Higher is better" if higher_is_better else "Lower is better"
    title_prefix = "Best-Branch Lineage" if is_best_default else "Branch Lineage"
    title = f"{title_prefix}: {branch_name} | {variable}"
    subtitle = (
        f"{direction_text}. Points: {point_count}. "
        f"Start {_format_plot_value(start)} -> End {_format_plot_value(end)}. "
        f"Improvement {_format_plot_value(improvement)}."
    )
    start_x, start_y = chart_points[0]
    end_x, end_y = chart_points[-1]
    annotations = [
        f'<text x="{start_x:.2f}" y="{max(margin_top + 16.0, start_y - 14.0):.2f}" text-anchor="middle" '
        'font-family="monospace" font-size="12" fill="#1f2937">'
        f"{escape(points[0]['branch_name'])} {_format_plot_value(start)}"
        "</text>",
        f'<text x="{end_x:.2f}" y="{max(margin_top + 16.0, end_y - 14.0):.2f}" text-anchor="middle" '
        'font-family="monospace" font-size="12" font-weight="700" fill="#0f766e">'
        f"{escape(points[-1]['branch_name'])} {_format_plot_value(end)}"
        "</text>",
    ]
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
            '<rect width="100%" height="100%" fill="#f8fafc" />',
            f'<rect x="{margin_left}" y="{margin_top}" width="{plot_width}" height="{plot_height}" rx="10" fill="white" stroke="#d1d5db" stroke-width="1" />',
            f'<text x="{margin_left}" y="28" font-family="monospace" font-size="20" fill="#111827">{escape(title)}</text>',
            f'<text x="{margin_left}" y="48" font-family="monospace" font-size="12" fill="#4b5563">{escape(subtitle)}</text>',
            *grid_lines,
            f'<line x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" y2="{height - margin_bottom}" stroke="#111827" stroke-width="1.5" />',
            f'<line x1="{margin_left}" y1="{height - margin_bottom}" x2="{width - margin_right}" y2="{height - margin_bottom}" stroke="#111827" stroke-width="1.5" />',
            *tick_labels,
            *x_tick_labels,
            f'<polyline fill="none" stroke="#2563eb" stroke-width="2.5" points="{polyline_points}" />',
            *point_elements,
            *annotations,
            f'<text x="{margin_left}" y="{height - 18}" font-family="monospace" font-size="12" fill="#374151">X-axis: lineage step from root to {escape(branch_name)}</text>',
            f'<text transform="translate(22 {margin_top + plot_height / 2:.2f}) rotate(-90)" font-family="monospace" font-size="12" fill="#374151">{escape(variable)}</text>',
            "</svg>",
            "",
        ]
    )


def _render_tree_svg(points, variable: str, focus_branch: str, is_best_default: bool) -> str:
    points_by_branch = {point["branch_name"]: point for point in points}
    children_by_parent = defaultdict(list)
    for point in points:
        children_by_parent[point["parent_branch_name"]].append(point["branch_name"])
    for child_names in children_by_parent.values():
        child_names.sort()

    leaf_count = 0
    y_positions: dict[str, float] = {}

    def assign_y(branch_name: str) -> float:
        nonlocal leaf_count
        child_names = children_by_parent.get(branch_name, [])
        if not child_names:
            y = float(leaf_count)
            leaf_count += 1
            y_positions[branch_name] = y
            return y
        child_ys = [assign_y(child_name) for child_name in child_names]
        y = sum(child_ys) / float(len(child_ys))
        y_positions[branch_name] = y
        return y

    root_names = children_by_parent.get(None, [])
    for root_name in root_names:
        assign_y(root_name)

    branch_order = sorted(points_by_branch, key=lambda name: (points_by_branch[name]["depth"], y_positions.get(name, 0.0), name))
    max_depth = max(int(point["depth"]) for point in points)
    leaf_slots = max(1, leaf_count)
    width = min(2400, max(1120, 380 + max_depth * 260))
    height = min(2800, max(680, 220 + leaf_slots * 82))
    margin_left = 110
    margin_right = 280
    margin_top = 84
    margin_bottom = 56
    plot_width = width - margin_left - margin_right
    plot_height = height - margin_top - margin_bottom
    x_step = 0.0 if max_depth == 0 else plot_width / float(max_depth)
    y_step = 0.0 if leaf_slots <= 1 else plot_height / float(leaf_slots - 1)

    x_positions = {
        branch_name: margin_left + float(points_by_branch[branch_name]["depth"]) * x_step
        for branch_name in points_by_branch
    }
    if leaf_slots <= 1:
        base_y = margin_top + plot_height / 2.0
        absolute_y_positions = {branch_name: base_y for branch_name in points_by_branch}
    else:
        absolute_y_positions = {
            branch_name: margin_top + y_positions.get(branch_name, 0.0) * y_step
            for branch_name in points_by_branch
        }

    available_values = [float(point["value"]) for point in points if point["value"] is not None]
    min_value = min(available_values)
    max_value = max(available_values)
    value_span = max_value - min_value

    def node_fill(value: float | None) -> str:
        if value is None:
            return "#cbd5e1"
        if value_span == 0:
            ratio = 0.5
        else:
            ratio = (float(value) - min_value) / value_span
        red = int(234 + (15 - 234) * ratio)
        green = int(179 + (118 - 179) * ratio)
        blue = int(8 + (110 - 8) * ratio)
        return f"#{red:02x}{green:02x}{blue:02x}"

    highlight_branches = set()
    current_branch = focus_branch
    while current_branch is not None and current_branch in points_by_branch:
        highlight_branches.add(current_branch)
        current_branch = points_by_branch[current_branch]["parent_branch_name"]

    depth_guides = []
    for depth in range(max_depth + 1):
        x = margin_left + depth * x_step
        depth_guides.append(
            f'<line x1="{x:.2f}" y1="{margin_top}" x2="{x:.2f}" y2="{height - margin_bottom}" stroke="#e5e7eb" stroke-width="1" />'
        )

    edge_elements = []
    for branch_name in branch_order:
        point = points_by_branch[branch_name]
        parent_name = point["parent_branch_name"]
        if parent_name is None or parent_name not in points_by_branch:
            continue
        is_highlight = branch_name in highlight_branches and parent_name in highlight_branches
        edge_elements.append(
            f'<line x1="{x_positions[parent_name]:.2f}" y1="{absolute_y_positions[parent_name]:.2f}" '
            f'x2="{x_positions[branch_name]:.2f}" y2="{absolute_y_positions[branch_name]:.2f}" '
            f'stroke="{"#0f766e" if is_highlight else "#94a3b8"}" '
            f'stroke-width="{"3" if is_highlight else "1.5"}" '
            f'stroke-opacity="{"0.95" if is_highlight else "0.7"}" />'
        )

    node_elements = []
    label_elements = []
    for branch_name in branch_order:
        point = points_by_branch[branch_name]
        value = point["value"]
        is_focus = branch_name == focus_branch
        is_highlight = branch_name in highlight_branches
        radius = 10.0 if is_focus else 7.0 if is_highlight else 6.0
        stroke = "#0f172a" if is_focus else "#0f766e" if is_highlight else "#334155"
        stroke_width = 2.5 if is_focus else 2.0 if is_highlight else 1.25
        node_elements.append(
            f'<circle cx="{x_positions[branch_name]:.2f}" cy="{absolute_y_positions[branch_name]:.2f}" '
            f'r="{radius:.1f}" fill="{node_fill(value)}" stroke="{stroke}" stroke-width="{stroke_width:.2f}">'
            f"<title>{escape(branch_name)} | status={escape(str(point['status']))} | {escape(variable)}={escape('n/a' if value is None else _format_plot_value(float(value)))}</title>"
            "</circle>"
        )
        value_label = "n/a" if value is None else _format_plot_value(float(value))
        label_font_weight = "700" if is_focus else "600" if is_highlight else "400"
        label_elements.append(
            f'<text x="{x_positions[branch_name] + 12:.2f}" y="{absolute_y_positions[branch_name] - 2:.2f}" '
            f'font-family="monospace" font-size="12" font-weight="{label_font_weight}" fill="#0f172a">'
            f"{escape(branch_name)}"
            "</text>"
        )
        label_elements.append(
            f'<text x="{x_positions[branch_name] + 12:.2f}" y="{absolute_y_positions[branch_name] + 14:.2f}" '
            'font-family="monospace" font-size="11" fill="#475569">'
            f"{escape(variable)}={escape(value_label)}"
            "</text>"
        )

    title = f"Search Tree: {variable}"
    subtitle = (
        f"Entire MCTS tree with {len(points)} nodes. "
        f"Highlight: {focus_branch}. "
        f"Color scale maps low to high {variable}; gray means unavailable."
    )
    caption = "default focus = current best branch" if is_best_default else "focus branch selected with --branch"
    legend_x = width - margin_right + 32
    legend_y = margin_top + 24
    legend_blocks = [
        f'<rect x="{legend_x}" y="{legend_y}" width="22" height="12" fill="{node_fill(min_value)}" stroke="#334155" stroke-width="0.8" />',
        f'<rect x="{legend_x}" y="{legend_y + 18}" width="22" height="12" fill="{node_fill((min_value + max_value) / 2.0)}" stroke="#334155" stroke-width="0.8" />',
        f'<rect x="{legend_x}" y="{legend_y + 36}" width="22" height="12" fill="{node_fill(max_value)}" stroke="#334155" stroke-width="0.8" />',
        f'<rect x="{legend_x}" y="{legend_y + 54}" width="22" height="12" fill="#cbd5e1" stroke="#334155" stroke-width="0.8" />',
        f'<text x="{legend_x + 30}" y="{legend_y + 10}" font-family="monospace" font-size="11" fill="#334155">low {_format_plot_value(min_value)}</text>',
        f'<text x="{legend_x + 30}" y="{legend_y + 28}" font-family="monospace" font-size="11" fill="#334155">mid {_format_plot_value((min_value + max_value) / 2.0)}</text>',
        f'<text x="{legend_x + 30}" y="{legend_y + 46}" font-family="monospace" font-size="11" fill="#334155">high {_format_plot_value(max_value)}</text>',
        f'<text x="{legend_x + 30}" y="{legend_y + 64}" font-family="monospace" font-size="11" fill="#334155">missing value</text>',
    ]

    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
            '<rect width="100%" height="100%" fill="#f8fafc" />',
            f'<rect x="{margin_left - 22}" y="{margin_top - 22}" width="{plot_width + 44}" height="{plot_height + 44}" rx="18" fill="white" stroke="#d1d5db" stroke-width="1" />',
            f'<text x="{margin_left}" y="30" font-family="monospace" font-size="22" fill="#111827">{escape(title)}</text>',
            f'<text x="{margin_left}" y="52" font-family="monospace" font-size="12" fill="#475569">{escape(subtitle)}</text>',
            f'<text x="{margin_left}" y="{height - 18}" font-family="monospace" font-size="11" fill="#475569">X-axis: branch depth from root. {escape(caption)}.</text>',
            *depth_guides,
            *edge_elements,
            *node_elements,
            *label_elements,
            *legend_blocks,
            "</svg>",
            "",
        ]
    )


def _format_plot_value(value: float) -> str:
    magnitude = abs(value)
    if magnitude >= 1000:
        return f"{value:,.0f}"
    if magnitude >= 100:
        return f"{value:,.1f}"
    if magnitude >= 10:
        return f"{value:,.2f}"
    if magnitude >= 1:
        return f"{value:,.4f}"
    return f"{value:.6f}"
