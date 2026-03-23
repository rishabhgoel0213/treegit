from __future__ import annotations

import argparse
import cProfile
import json
import os
from pathlib import Path
import platform
import resource
import pstats
import statistics
import sys
import tempfile
import time


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from treegit.repository import Repository  # noqa: E402


DEFAULT_CONTENT = "alpha beta gamma delta\n" * 8
SCENARIO_KEYS = {
    "initial_commit": "initial_commit_s",
    "warm_status": "warm_status_s",
    "warm_noop_commit": "warm_noop_commit_s",
    "warm_one_change_commit": "warm_one_change_commit_s",
    "checkout_root": "checkout_root_s",
    "checkout_feature": "checkout_feature_s",
    "diff_noop": "diff_noop_s",
    "diff_commit": "diff_commit_s",
    "search_content": "search_content_s",
    "search_path": "search_path_s",
    "search_commit": "search_commit_s",
    "search_branch": "search_branch_s",
    "search_deep_content": "search_deep_content_s",
}
DEFAULT_SCENARIOS = list(SCENARIO_KEYS)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark TreeGit command latency and compare against a baseline.")
    parser.add_argument("--files", type=int, default=5000, help="Number of tracked files to generate.")
    parser.add_argument("--fanout", type=int, default=200, help="Files per generated directory.")
    parser.add_argument(
        "--history-depth",
        type=int,
        default=50,
        help="Additional one-file commits to create for the deep-history search scenario.",
    )
    parser.add_argument("--warmups", type=int, default=2, help="Number of warmup repetitions to discard.")
    parser.add_argument("--repeats", type=int, default=7, help="Number of measured repetitions.")
    parser.add_argument(
        "--scenario",
        action="append",
        choices=DEFAULT_SCENARIOS,
        help="Benchmark a specific scenario. Repeat to select multiple scenarios. Defaults to all.",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format.",
    )
    parser.add_argument(
        "--compare",
        type=Path,
        help="Compare the current summary against a baseline JSON file produced by this script.",
    )
    parser.add_argument(
        "--fail-on-regression-pct",
        type=float,
        default=None,
        help="Exit with status 1 when any compared scenario regresses beyond this median percentage.",
    )
    parser.add_argument(
        "--profile",
        choices=DEFAULT_SCENARIOS,
        help="Print a cProfile report for a single scenario.",
    )
    parser.add_argument(
        "--profile-lines",
        type=int,
        default=20,
        help="Number of cProfile rows to print when --profile is set.",
    )
    return parser.parse_args()


def populate(root: Path, files: int, fanout: int) -> None:
    for index in range(files):
        path = root / f"src/module_{index // fanout:03d}/file_{index:05d}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{DEFAULT_CONTENT}{index}\n", encoding="utf-8")


def measure(func) -> float:
    started = time.perf_counter()
    func()
    return time.perf_counter() - started


def extend_history(root: Path, repo: Repository, depth: int) -> None:
    if depth <= 0:
        return
    target = root / "src/module_000/file_00000.txt"
    for index in range(depth):
        target.write_text(f"{DEFAULT_CONTENT}historytoken{index}\n", encoding="utf-8")
        repo.commit(f"history step {index}")


def run_single(files: int, fanout: int, history_depth: int, scenarios: set[str]) -> dict[str, float]:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        repo = Repository.init(root)
        populate(root, files, fanout)

        results: dict[str, float] = {}
        initial_commit = measure(lambda: repo.commit("initial snapshot"))
        root_commit = repo.head_commit_id()
        assert root_commit is not None
        if "initial_commit" in scenarios:
            results[SCENARIO_KEYS["initial_commit"]] = initial_commit

        if "warm_status" in scenarios:
            results[SCENARIO_KEYS["warm_status"]] = measure(repo.status)
        noop_commit = measure(lambda: repo.commit("noop snapshot"))
        root_commit = repo.head_commit_id()
        assert root_commit is not None
        if "warm_noop_commit" in scenarios:
            results[SCENARIO_KEYS["warm_noop_commit"]] = noop_commit

        repo.create_branch("feature")
        repo.checkout("feature", force=True)
        target = root / "src/module_000/file_00000.txt"
        target.write_text(f"{DEFAULT_CONTENT}changed\n", encoding="utf-8")
        feature_commit_time = measure(lambda: repo.commit("feature work"))
        feature_commit = repo.head_commit_id()
        assert feature_commit is not None
        if "warm_one_change_commit" in scenarios:
            results[SCENARIO_KEYS["warm_one_change_commit"]] = feature_commit_time

        if "checkout_root" in scenarios:
            results[SCENARIO_KEYS["checkout_root"]] = measure(lambda: repo.checkout("root", force=True))
        elif "checkout_feature" in scenarios:
            repo.checkout("root", force=True)

        if "checkout_feature" in scenarios:
            results[SCENARIO_KEYS["checkout_feature"]] = measure(lambda: repo.checkout("feature", force=True))

        if repo.current_branch() != "feature":
            repo.checkout("feature", force=True)

        if "diff_noop" in scenarios:
            results[SCENARIO_KEYS["diff_noop"]] = measure(repo.diff)
        if "diff_commit" in scenarios:
            results[SCENARIO_KEYS["diff_commit"]] = measure(lambda: repo.diff(root_commit, feature_commit))
        if "search_content" in scenarios:
            results[SCENARIO_KEYS["search_content"]] = measure(lambda: repo.search("changed", field="content"))
        if "search_path" in scenarios:
            results[SCENARIO_KEYS["search_path"]] = measure(lambda: repo.search("file_00000", field="path"))
        if "search_commit" in scenarios:
            results[SCENARIO_KEYS["search_commit"]] = measure(lambda: repo.search("feature", field="commit"))
        if "search_branch" in scenarios:
            results[SCENARIO_KEYS["search_branch"]] = measure(lambda: repo.search("feature", field="branch"))
        if "search_deep_content" in scenarios:
            extend_history(root, repo, history_depth)
            results[SCENARIO_KEYS["search_deep_content"]] = measure(
                lambda: repo.search(
                    f"historytoken{max(1, history_depth) - 1}",
                    field="content",
                    branch="feature",
                )
            )

        results["maxrss_kb"] = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return results


def profile_single(files: int, fanout: int, history_depth: int, scenario: str, lines: int) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        repo = Repository.init(root)
        populate(root, files, fanout)

        if scenario == "initial_commit":
            action = lambda: repo.commit("profile-initial")  # noqa: E731
        else:
            repo.commit("initial snapshot")
            repo.commit("noop snapshot")
            repo.create_branch("feature")
            repo.checkout("feature", force=True)
            target = root / "src/module_000/file_00000.txt"
            target.write_text(f"{DEFAULT_CONTENT}changed\n", encoding="utf-8")
            repo.commit("feature work")
            root_commit = repo.resolve_revision("root")
            feature_commit = repo.resolve_revision("feature")
            if scenario == "warm_one_change_commit":
                target.write_text(f"{DEFAULT_CONTENT}profile-changed\n", encoding="utf-8")
            if scenario == "search_deep_content":
                extend_history(root, repo, history_depth)
            if scenario == "checkout_feature":
                repo.checkout("root", force=True)
            action = {
                "warm_status": repo.status,
                "warm_noop_commit": lambda: repo.commit("profile-noop"),
                "warm_one_change_commit": lambda: repo.commit("profile-one-change"),
                "checkout_root": lambda: repo.checkout("root", force=True),
                "checkout_feature": lambda: repo.checkout("feature", force=True),
                "diff_noop": repo.diff,
                "diff_commit": lambda: repo.diff(root_commit, feature_commit),
                "search_content": lambda: repo.search("changed", field="content"),
                "search_path": lambda: repo.search("file_00000", field="path"),
                "search_commit": lambda: repo.search("feature", field="commit"),
                "search_branch": lambda: repo.search("feature", field="branch"),
                "search_deep_content": lambda: repo.search(
                    f"historytoken{max(1, history_depth) - 1}",
                    field="content",
                    branch="feature",
                ),
            }[scenario]

        profiler = cProfile.Profile()
        profiler.enable()
        action()
        profiler.disable()
        stats = pstats.Stats(profiler).sort_stats("cumtime")
        stats.print_stats(lines)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def summarize(samples: list[dict[str, float]]) -> dict[str, dict[str, float]]:
    keys = samples[0].keys()
    summary: dict[str, dict[str, float]] = {}
    for key in keys:
        values = [sample[key] for sample in samples]
        summary[key] = {
            "mean": statistics.fmean(values),
            "median": statistics.median(values),
            "p90": percentile(values, 0.9),
            "min": min(values),
            "max": max(values),
        }
    return summary


def compare_summary(
    summary: dict[str, dict[str, float]],
    baseline_path: Path | None,
    fail_on_regression_pct: float | None,
) -> tuple[dict[str, dict[str, float]], bool]:
    if baseline_path is None:
        return {}, False
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline_summary = baseline.get("summary", {})
    comparison: dict[str, dict[str, float]] = {}
    regressed = False
    for key, metrics in summary.items():
        baseline_metrics = baseline_summary.get(key)
        if not isinstance(baseline_metrics, dict):
            continue
        baseline_median = float(baseline_metrics["median"])
        current_median = float(metrics["median"])
        if baseline_median == 0:
            delta_pct = 0.0
        else:
            delta_pct = ((current_median - baseline_median) / baseline_median) * 100.0
        comparison[key] = {
            "baseline_median": baseline_median,
            "current_median": current_median,
            "delta_pct": delta_pct,
        }
        if fail_on_regression_pct is not None and delta_pct > fail_on_regression_pct:
            regressed = True
    return comparison, regressed


def print_text(
    summary: dict[str, dict[str, float]],
    comparison: dict[str, dict[str, float]],
    files: int,
    fanout: int,
    history_depth: int,
    repeats: int,
    warmups: int,
) -> None:
    print(
        "TreeGit benchmark:"
        f" files={files}, fanout={fanout}, history_depth={history_depth}, warmups={warmups}, repeats={repeats}"
    )
    for name, metrics in summary.items():
        label = name.removesuffix("_s")
        if name.endswith("_s"):
            print(
                f"{label:>24}: median={metrics['median']:.4f}s p90={metrics['p90']:.4f}s"
                f" min={metrics['min']:.4f}s max={metrics['max']:.4f}s"
            )
        else:
            print(
                f"{label:>24}: median={metrics['median']:.1f} p90={metrics['p90']:.1f}"
                f" min={metrics['min']:.1f} max={metrics['max']:.1f}"
            )
    for name, metrics in comparison.items():
        print(
            f"{name.removesuffix('_s'):>24}: baseline_median={metrics['baseline_median']:.4f}"
            f" current_median={metrics['current_median']:.4f}"
            f" delta={metrics['delta_pct']:+.1f}%"
        )


def main() -> None:
    args = parse_args()
    scenarios = set(args.scenario or DEFAULT_SCENARIOS)
    if args.profile:
        profile_single(args.files, args.fanout, args.history_depth, args.profile, args.profile_lines)
        return

    for _ in range(args.warmups):
        run_single(args.files, args.fanout, args.history_depth, scenarios)

    samples = [run_single(args.files, args.fanout, args.history_depth, scenarios) for _ in range(args.repeats)]
    summary = summarize(samples)
    comparison, regressed = compare_summary(summary, args.compare, args.fail_on_regression_pct)
    payload = {
        "config": {
            "files": args.files,
            "fanout": args.fanout,
            "history_depth": args.history_depth,
            "warmups": args.warmups,
            "repeats": args.repeats,
            "scenarios": sorted(scenarios),
        },
        "environment": {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cwd": os.fspath(REPO_ROOT),
        },
        "samples": samples,
        "summary": summary,
    }
    if comparison:
        payload["comparison"] = comparison
    if args.format == "json":
        print(json.dumps(payload, separators=(",", ":")))
    else:
        print_text(summary, comparison, args.files, args.fanout, args.history_depth, args.repeats, args.warmups)
    if regressed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
