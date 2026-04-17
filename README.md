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
- `treegit metric define <name>` creates a shared branch metric initialized to `0.0` on all existing and future branches.
- `treegit metric get <name>` reads the metric for the current branch.
- `treegit metric backprop <name> <value>` increments the metric on the current branch and all of its parents.
- `treegit branch` prints the branch tree.
- `treegit mcts ...` runs a synchronous MCTS loop over branches using pluggable expander and objective commands.

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

## Benchmarking

Run the synthetic large-repo benchmark from the repository root:

```bash
python3 benchmarks/commit_perf.py --files 5000 --warmups 2 --repeats 7
```

To focus on a subset of hot paths:

```bash
python3 benchmarks/commit_perf.py --files 5000 \
  --scenario warm_status \
  --scenario warm_noop_commit \
  --scenario warm_one_change_commit \
  --scenario checkout_root \
  --scenario checkout_feature \
  --scenario diff_noop
```

To compare a run against a saved baseline:

```bash
python3 benchmarks/commit_perf.py --files 5000 \
  --compare /path/to/baseline.json \
  --fail-on-regression-pct 10
```

To inspect a hot path with `cProfile`:

```bash
python3 benchmarks/commit_perf.py --files 5000 --profile diff_noop
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
treegit metric define <name>
treegit metric get <name>
treegit metric backprop <name> <value>
treegit mcts init --config /path/to/config.json [--run-id RUN]
treegit mcts step <run_id>
treegit mcts run <run_id> [--steps N]
treegit mcts status <run_id>
treegit mcts best <run_id>
treegit mcts plot [--view lineage|tree] [--var name] [--branch name] [--output path]
```

## MCTS Config

The MCTS entrypoint is command-based and objective-agnostic. The config file is JSON and must define:

- `root_branch`
- `worktree_root`
- `branch_prefix`
- `iteration_budget` (`expansion_width` is accepted as a legacy alias)
- `selection.policy`, `selection.exploration_constant`, `selection.widening_coefficient`, `selection.widening_exponent`, and `selection.virtual_loss`
- `expander.command`
- `objective.id`, `objective.version`, and `objective.command`

The expander command runs in a fresh worktree for each allocated budget slot. TreeGit uses a budgeted UCB selector with progressive widening, so one iteration can split work across multiple parent branches or spend multiple slots on the same parent when that still scores best. Objective commands still report raw `utility`, but TreeGit now backpropagates a clipped robust z-score normalization of observed utilities so exploration constants and virtual loss stay meaningful across objectives with very different score scales.

## Notes

- Ignored files can be listed in `.treegitignore`.
- `.treegit/` internals are never tracked.
- Additional worktrees keep their own local branch selection while sharing the same objects and index.
- Metrics are shared across worktrees because they live in the shared index.
- Search is local and indexed with SQLite.
- Binary files are tracked, but content search only indexes text files up to 1 MiB.
- `--force` is only needed when you want checkout to discard uncommitted local changes.
