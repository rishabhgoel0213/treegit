from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

from treegit.errors import TreeGitError
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
    return 0
