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
            print(f"selected: {result.selected_branch}")
            for child in result.children:
                utility = "None" if child.utility is None else f"{child.utility:.6f}"
                print(
                    f"{child.branch_name} status={child.status} commit={child.commit_id or ''} "
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
                print(f"last_selected: {results[-1].selected_branch}", flush=True)
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
            output_path, best_branch, point_count = _write_best_path_plot(primary_repo, engine, args.output)
            print(f"output: {output_path}")
            print(f"best_branch: {best_branch}")
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
        print(f"selected: {result.selected_branch}", flush=True)
        for child in result.children:
            utility = "None" if child.utility is None else f"{child.utility:.6f}"
            print(
                f"{child.branch_name} status={child.status} commit={child.commit_id or ''} "
                f"utility={utility} reason={child.reason or ''}".rstrip(),
                flush=True,
            )
        print(f"frontier_count: {result.frontier_count}", flush=True)
        if result.best_branch is not None:
            print(f"best_branch: {result.best_branch}", flush=True)
    return results


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


def _write_best_path_plot(repo: Repository, engine: MCTSEngine, raw_output_path: str | None) -> tuple[Path, str, int]:
    best = engine.best()
    if best is None or best.last_raw_score is None:
        raise TreeGitError("no evaluated MCTS branches to plot")
    nodes = {node.branch_name: node for node in engine.store.list_nodes()}
    lineage = []
    current = best
    while current is not None:
        if current.last_raw_score is not None:
            lineage.append(current)
        current = None if current.parent_branch_name is None else nodes.get(current.parent_branch_name)
    lineage.reverse()
    output_path = _resolve_plot_path(repo, raw_output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_render_best_path_svg(lineage), encoding="utf-8")
    return output_path, best.branch_name, len(lineage)


def _resolve_plot_path(repo: Repository, raw_path: str | None) -> Path:
    if raw_path is None:
        return (repo.git_dir / "mcts-best-path.svg").resolve()
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (repo.root / path).resolve()
    return path


def _render_best_path_svg(lineage) -> str:
    width = 960
    height = 540
    margin_left = 90
    margin_right = 40
    margin_top = 60
    margin_bottom = 110
    plot_width = width - margin_left - margin_right
    plot_height = height - margin_top - margin_bottom

    scores = [float(node.last_raw_score) for node in lineage]
    min_score = min(scores)
    max_score = max(scores)
    if min_score == max_score:
        min_score -= 1.0
        max_score += 1.0
    score_span = max_score - min_score

    if len(lineage) == 1:
        x_positions = [margin_left + plot_width / 2.0]
    else:
        x_step = plot_width / float(len(lineage) - 1)
        x_positions = [margin_left + i * x_step for i in range(len(lineage))]

    def y_for_score(score: float) -> float:
        normalized = (score - min_score) / score_span
        return margin_top + normalized * plot_height

    points = [(x, y_for_score(float(node.last_raw_score))) for x, node in zip(x_positions, lineage)]
    polyline_points = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)

    grid_lines = []
    tick_labels = []
    tick_count = 5
    for index in range(tick_count + 1):
        ratio = index / tick_count
        score = min_score + ratio * score_span
        y = margin_top + ratio * plot_height
        grid_lines.append(
            f'<line x1="{margin_left}" y1="{y:.2f}" x2="{width - margin_right}" y2="{y:.2f}" '
            'stroke="#e5e7eb" stroke-width="1" />'
        )
        tick_labels.append(
            f'<text x="{margin_left - 12}" y="{y + 4:.2f}" text-anchor="end" '
            'font-family="monospace" font-size="12" fill="#374151">'
            f"{escape(f'{score:.0f}')}"
            "</text>"
        )

    point_elements = []
    for (x, y), node in zip(points, lineage):
        score = float(node.last_raw_score)
        label = node.branch_name
        point_elements.append(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4.5" fill="#2563eb" stroke="white" stroke-width="1.5" />'
        )
        point_elements.append(
            f'<text x="{x:.2f}" y="{height - margin_bottom + 24:.2f}" text-anchor="middle" '
            'font-family="monospace" font-size="11" fill="#111827">'
            f"{escape(label)}"
            "</text>"
        )
        point_elements.append(
            f'<text x="{x:.2f}" y="{max(margin_top + 14.0, y - 10.0):.2f}" text-anchor="middle" '
            'font-family="monospace" font-size="11" fill="#1f2937">'
            f"{escape(f'{score:.0f}')}"
            "</text>"
        )

    title = "Best-Branch Lineage Score History"
    subtitle = f"Lower is better. Branch count: {len(lineage)}"
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
            '<rect width="100%" height="100%" fill="white" />',
            f'<text x="{margin_left}" y="28" font-family="monospace" font-size="20" fill="#111827">{escape(title)}</text>',
            f'<text x="{margin_left}" y="48" font-family="monospace" font-size="12" fill="#4b5563">{escape(subtitle)}</text>',
            *grid_lines,
            f'<line x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" y2="{height - margin_bottom}" stroke="#111827" stroke-width="1.5" />',
            f'<line x1="{margin_left}" y1="{height - margin_bottom}" x2="{width - margin_right}" y2="{height - margin_bottom}" stroke="#111827" stroke-width="1.5" />',
            *tick_labels,
            f'<polyline fill="none" stroke="#2563eb" stroke-width="2.5" points="{polyline_points}" />',
            *point_elements,
            f'<text x="{margin_left}" y="{height - 18}" font-family="monospace" font-size="12" fill="#374151">Lineage order from root child to current best</text>',
            f'<text transform="translate(18 {margin_top + plot_height / 2:.2f}) rotate(-90)" font-family="monospace" font-size="12" fill="#374151">score</text>',
            "</svg>",
            "",
        ]
    )
