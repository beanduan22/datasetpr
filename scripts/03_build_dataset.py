"""Stage 3: aggregate mined repos into dataset instances + summary stats.

Instance = a set of >= MIN_K merged agent PRs that
  * share exactly the same fork commit (same base), and
  * form one connected component in the pairwise code-conflict graph
    (lock files / generated files ignored).

Ground truth per instance:
  * per PR: landing commit on target branch and sync-merge commits
  * final: landing commit of the last-merged PR in the component
Output: data/dataset.jsonl, data/summary.json
"""
import argparse
import json
import re
import statistics
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SOURCE_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".go", ".rs",
    ".java", ".kt", ".kts", ".scala", ".cs", ".fs", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp",
    ".m", ".mm", ".swift", ".dart", ".rb", ".php", ".lua", ".ex", ".exs", ".erl", ".hs", ".ml",
    ".clj", ".r", ".jl", ".zig", ".sol", ".sh", ".ps1", ".psm1", ".tf", ".gd", ".sql", ".proto",
}
GENERATED_RE = re.compile(r"(\.min\.js$|\.pb\.go$|_pb2\.py$|\.g\.dart$|\.generated\.|/generated/|/dist/|/vendor/)")


def is_source(path: str) -> bool:
    ext = Path(path).suffix.lower()
    return ext in SOURCE_EXT and not GENERATED_RE.search(path)


def conflict_components(members: list[int], pairs: list[dict]) -> list[list[int]]:
    parent = {n: n for n in members}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for p in pairs:
        parent[find(p["a"])] = find(p["b"])
    comps: dict[int, list[int]] = {}
    for n in members:
        comps.setdefault(find(n), []).append(n)
    return sorted(comps.values(), key=len, reverse=True)


def filter_pairs(pairs: list[dict], code_only: bool) -> list[dict]:
    """Keep pairs with >=1 conflict hunk in a kept file; restrict files accordingly."""
    out = []
    for p in pairs:
        files = [f for f in p["code_files"] if not code_only or is_source(f)]
        files = [f for f in files if p["hunks"].get(f, 0) > 0]
        if files:
            out.append({**p, "code_files": files,
                        "n_code_hunks": sum(p["hunks"][f] for f in files)})
    return out


def load_landing_api() -> dict[tuple[str, int], dict]:
    """(repo, pr) -> API row from 05_fill_landing.py, with target_is_default resolved."""
    f, fd = ROOT / "data" / "landing_api.jsonl", ROOT / "data" / "default_branch.json"
    if not f.exists() or not fd.exists():
        raise SystemExit("run scripts/05_fill_landing.py first (landing_api.jsonl + default_branch.json)")
    default = json.loads(fd.read_text())
    out = {}
    for r in map(json.loads, f.open()):
        if r.get("merged"):
            out[(r["repo"], r["pr"])] = {**r, "target_is_default": r["base_ref"] == default.get(r["repo"])}
    return out


def apply_landing_api(repo: dict, group: dict, api: dict) -> dict:
    """Fill landing only for PRs merged into the default branch; record each PR's target branch.

    PRs merged into another branch got their fork point from the default branch at merge time
    (02 fallback), so their "same base" is unreliable -> they keep landing = None.
    """
    prs = []
    for p in group["prs"]:
        if p["landing"]:  # found on default-branch first-parent history
            prs.append({**p, "landing_source": "local", "target_branch": "<default>"})
            continue
        r = api.get((repo["full_name"], p["number"]))
        if r is None:
            prs.append({**p, "landing_source": None, "target_branch": None})
        elif r["target_is_default"]:
            prs.append({**p, "landing": r["merge_commit_sha"], "landing_source": "github_api",
                        "target_branch": "<default>"})
        else:
            prs.append({**p, "landing_source": None, "target_branch": r["base_ref"]})
    return {**group, "prs": prs}


def load_test_info() -> dict[tuple[str, str, int], list[str]]:
    """(repo, base_commit, pr_number) -> test files changed by that PR (from 04_test_filter)."""
    info = {}
    for f in (ROOT / "data" / "test_filter").glob("*.json"):
        d = json.loads(f.read_text())
        for g in d.get("groups", []):
            for p in g["prs"]:
                if p["files_ok"]:
                    info[(d["full_name"], g["base"], p["number"])] = p["test_files"]
    return info


def add_test_fields(inst: dict, test_info: dict) -> dict:
    branches = []
    for b in inst["branches"]:
        tf = test_info.get((inst["repo"], inst["base_commit"], b["pr_number"]))
        branches.append({**b, "test_files": tf, "touches_tests": None if tf is None else bool(tf)})
    n = sum(bool(b["touches_tests"]) for b in branches)
    return {**inst, "branches": branches, "n_branches_with_tests": n,
            "all_branches_touch_tests": n == len(branches)}


def build_instance(repo: dict, group: dict, comp: set, kept_pairs: list[dict]) -> dict:
    prs = sorted((p for p in group["prs"] if p["number"] in comp), key=lambda p: p["merged_at"])
    pairs = [p for p in kept_pairs if p["a"] in comp and p["b"] in comp]
    files = sorted({f for p in pairs for f in p["code_files"]})
    return {
        "instance_id": f"{repo['full_name'].replace('/', '__')}-{group['base'][:10]}-{min(comp)}",
        "repo": repo["full_name"],
        "language": repo.get("language"),
        "stars": repo.get("stars"),
        "base_commit": group["base"],
        "k": len(prs),
        "agents": dict(Counter(p["agent"] for p in prs)),
        "branches": [
            {
                "pr_number": p["number"],
                "pr_url": f"https://github.com/{repo['full_name']}/pull/{p['number']}",
                "title": p["title"],
                "agent": p["agent"],
                "created_at": p["created_at"],
                "merged_at": p["merged_at"],
                "orig_head": p["orig_head"],
                "final_head": p["head"],
                "n_commits": p["n_commits"],
                "sync_merges": p["sync_merges"],
                "landing_commit": p["landing"],
                "landing_source": p.get("landing_source", "local" if p["landing"] else None),
                "target_branch": p.get("target_branch"),
            }
            for p in prs
        ],
        "conflict_pairs": [
            {"a": p["a"], "b": p["b"], "files": p["code_files"],
             "hunks": {f: p["hunks"][f] for f in p["code_files"]}, "types": p["types"]}
            for p in pairs
        ],
        "n_conflict_pairs": len(pairs),
        "n_conflict_hunks": sum(p["n_code_hunks"] for p in pairs),
        "conflict_files": files,
        "gold_final_commit": prs[-1]["landing"] or prs[-1]["before_merge"],
        "gold_all_landed": all(p["landing"] for p in prs),
        "all_target_default": all(p.get("target_branch") == "<default>" for p in prs),
    }


def describe(xs: list) -> dict:
    if not xs:
        return {}
    xs = sorted(xs)
    q = lambda f: xs[min(len(xs) - 1, int(f * len(xs)))]
    return {"n": len(xs), "min": xs[0], "p25": q(.25), "median": q(.5), "p75": q(.75),
            "max": xs[-1], "mean": round(statistics.mean(xs), 2)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-k", type=int, default=5)
    ap.add_argument("--code-only", action="store_true", help="only count conflicts in source files")
    ap.add_argument("--out", default="dataset.jsonl")
    ap.add_argument("--tests", action="store_true",
                    help="annotate branches with test files from data/test_filter (run 04 first)")
    ap.add_argument("--landing-api", action="store_true",
                    help="fill missing landing commits from data/landing_api.jsonl (run 05 first)")
    args = ap.parse_args()

    repos, errors = [], Counter()
    for f in sorted((ROOT / "data" / "mined").glob("*.json")):
        r = json.loads(f.read_text())
        if "error" in r:
            errors[r["error"].split(":")[0]] += 1
            continue
        repos.append(r)

    same_base_groups = [g for r in repos for g in r["groups"]]
    api = load_landing_api() if args.landing_api else {}
    instances = []
    for r in repos:
        for g in r["groups"]:
            g = apply_landing_api(r, g, api) if api else g
            kept = filter_pairs(g["pairs"], args.code_only)
            members = [p["number"] for p in g["prs"]]
            for comp in conflict_components(members, kept):
                if len(comp) >= args.min_k:
                    instances.append(build_instance(r, g, set(comp), kept))
    instances.sort(key=lambda x: (-x["k"], -x["n_conflict_hunks"]))
    if args.tests:
        test_info = load_test_info()
        instances = [add_test_fields(x, test_info) for x in instances]

    with open(ROOT / "data" / args.out, "w") as f:
        for x in instances:
            f.write(json.dumps(x) + "\n")

    summary = {
        "repos_mined": len(repos),
        "repos_failed": dict(errors),
        "same_base_groups_ge5": len(same_base_groups),
        "same_base_groups_with_any_conflict": sum(g["n_code_conflict_pairs"] > 0 for g in same_base_groups),
        "instances_conflict_component_ge_k": len(instances),
        "instances_repos": len({x["repo"] for x in instances}),
        "k": describe([x["k"] for x in instances]),
        "conflict_hunks": describe([x["n_conflict_hunks"] for x in instances]),
        "conflict_pairs": describe([x["n_conflict_pairs"] for x in instances]),
        "conflict_files": describe([len(x["conflict_files"]) for x in instances]),
        "agents": dict(sum((Counter(x["agents"]) for x in instances), Counter())),
        "multi_agent_instances": sum(len(x["agents"]) > 1 for x in instances),
        "languages": dict(Counter(x["language"] for x in instances).most_common(12)),
        "gold_all_landed": sum(x["gold_all_landed"] for x in instances),
        "k_ge": {k: sum(x["k"] >= k for x in instances) for k in (5, 8, 9, 10, 15, 20)},
    }
    if args.landing_api:
        ok = [x for x in instances if x["gold_all_landed"] and x["all_target_default"]]
        summary["all_target_default"] = sum(x["all_target_default"] for x in instances)
        summary["reliable_gold"] = {
            "instances": len(ok), "repos": len({x["repo"] for x in ok}),
            "k_ge": {k: sum(x["k"] >= k for x in ok) for k in (9, 10, 15, 20)},
            "every_pr_touches_tests": sum(x.get("all_branches_touch_tests", False) for x in ok),
            "every_pr_touches_tests_k_ge_10": sum(
                x.get("all_branches_touch_tests", False) and x["k"] >= 10 for x in ok),
            "languages": dict(Counter(x["language"] for x in ok).most_common(12))}
    if args.tests:
        tested = [x for x in instances if x["all_branches_touch_tests"]]
        summary["missing_test_info_branches"] = sum(
            b["touches_tests"] is None for x in instances for b in x["branches"])
        summary["all_branches_touch_tests"] = {
            "instances": len(tested), "repos": len({x["repo"] for x in tested}),
            "k_ge": {k: sum(x["k"] >= k for x in tested) for k in (9, 10, 15, 20)},
            "languages": dict(Counter(x["language"] for x in tested).most_common(12))}
    (ROOT / "data" / args.out.replace("dataset", "summary").replace(".jsonl", ".json")).write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
