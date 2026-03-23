from __future__ import annotations

import argparse
from collections import defaultdict
import os
from pathlib import Path
import subprocess
import sys
import time

from treegit.errors import TreeGitError
from treegit.mcts import MCTSEngine
from treegit.repository import Repository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="treegit")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init")
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

    mcts_subparsers.add_parser("status")

    mcts_subparsers.add_parser("best")

    mcts_inspect_parser = mcts_subparsers.add_parser("inspect")
    mcts_inspect_parser.add_argument("branch")

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
        engine = MCTSEngine(repo)
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
            if args.background:
                log_path, pid = _spawn_background_mcts_run(repo, steps=args.steps, log_file=args.log_file)
                print(f"background_pid: {pid}")
                print(f"log_file: {log_path}")
                return 0
            results = _run_mcts_steps(engine, args.steps)
            print(f"steps_executed: {len(results)}", flush=True)
            if results:
                print(f"last_selected: {results[-1].selected_branch}", flush=True)
                if results[-1].best_branch is not None:
                    print(f"best_branch: {results[-1].best_branch}", flush=True)
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
    command = [sys.executable, "-m", "treegit", "mcts", "run", "--steps", str(steps)]
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
    return log_path, process.pid


def _resolve_log_path(repo: Repository, raw_path: str | None) -> Path:
    if raw_path is None:
        return (repo.git_dir / "mcts-run.log").resolve()
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (repo.root / path).resolve()
    return path
