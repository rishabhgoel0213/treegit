# TreeGit

TreeGit is a stripped-down, local-only version control tool for exploring a tree of codebase variants.

It is intentionally simpler than Git:

- local only
- no remotes
- no merge or rebase
- no staging area
- explicit branch tree structure
- commits snapshot the full working tree

## Current behavior

- `treegit init` creates a `.treegit/` repository and immediately checks out a `root` branch.
- `treegit commit -m "..."` snapshots the entire current working tree.
- `treegit branch <name>` creates a child branch from the current branch.
- `treegit worktree add <path> <branch>` creates another folder bound to a branch in the same repository.
  Re-running it for an existing linked worktree folder moves that folder to the requested branch.
- `treegit checkout <branch>` only works with branch names.
- You can check out `root` from anywhere; otherwise checkout is limited to your parent branch or a direct child branch.
- `treegit branch` prints the branch tree.

There is no `treegit add`.

## Running it

From this repository:

```bash
cd /home/rishabh/tree-git
PYTHONPATH=src python3 -m treegit --help
```

If you want a shell shortcut:

```bash
alias treegit='PYTHONPATH=/home/rishabh/tree-git/src python3 -m treegit'
```

## Basic workflow

```bash
treegit init

# edit files
treegit status
treegit commit -m "initial snapshot"

treegit branch feature
treegit worktree add ../feature feature
treegit checkout feature

# edit files on feature
treegit commit -m "feature work"

treegit branch leaf
treegit checkout leaf
```

To move between sibling branches, go through the parent branch:

```bash
treegit checkout feature
treegit checkout root
treegit checkout other-feature
```

## Commands

```text
treegit init
treegit status
treegit commit -m "message"
treegit log [revision]
treegit branch [name]
treegit worktree add <path> <branch>
treegit checkout <branch> [--force]
treegit diff [left] [right]
treegit search <query> [--field content|path|commit|branch|all] [--branch name] [--path glob] [--limit N]
```

## Notes

- Ignored files can be listed in `.treegitignore`.
- `.treegit/` internals are never tracked.
- Additional worktrees keep their own local branch selection while sharing the same objects and index.
- Search is local and indexed with SQLite.
- Binary files are tracked, but content search only indexes text files up to 1 MiB.
- `--force` is only needed when you want checkout to discard uncommitted local changes.
