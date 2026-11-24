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
    args = ap.parse_args()

    repos, errors = [], Counter()
    for f in sorted((ROOT / "data" / "mined").glob("*.json")):
        r = json.loads(f.read_text())
        if "error" in r:
            errors[r["error"].split(":")[0]] += 1
            continue
        repos.append(r)

    same_base_groups = [g for r in repos for g in r["groups"]]
    instances = []
    for r in repos:
        for g in r["groups"]:
            kept = filter_pairs(g["pairs"], args.code_only)
            members = [p["number"] for p in g["prs"]]
            for comp in conflict_components(members, kept):
                if len(comp) >= args.min_k:
                    instances.append(build_instance(r, g, set(comp), kept))
    instances.sort(key=lambda x: (-x["k"], -x["n_conflict_hunks"]))

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
        "k_ge": {k: sum(x["k"] >= k for x in instances) for k in (5, 8, 10, 15, 20)},
    }
    (ROOT / "data" / args.out.replace("dataset", "summary").replace(".jsonl", ".json")).write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
