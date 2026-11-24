"""Stage 4: SWE-bench-style pre-filter -- which conflict components have PRs that touch tests?

For every mined repo with a conflict component >= MIN_K:
  1. list files changed by each PR in the component: `git diff --name-only fork orig_head`
     (reuses data/repos/<repo> if still present, else a blob-less bare clone)
  2. classify each PR as touching test files / touching source files
  3. restrict the code-conflict graph to test-touching PRs and recompute the
     largest connected component (two edge definitions: "lock" = 02 default,
     "src" = 03 code_only, i.e. conflict hunks in source files only)

Output: data/test_filter/<owner>__<repo>.json (resumable), summary via --summary.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from importlib import import_module  # noqa: E402

build = import_module("03_build_dataset")

ROOT = Path(__file__).resolve().parent.parent
MINED = ROOT / "data" / "mined"
REPOS = ROOT / "data" / "repos"
LITE = ROOT / "data" / "repos_lite"
OUT = ROOT / "data" / "test_filter"

TEST_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs|e2e|integration[-_]tests?)/|\.(Unit)?Tests?/"
    r"|(^|/)test_[^/]+\.py$|_test\.(py|go|rb|exs?)$|[._-](test|spec)\.[cm]?[jt]sx?$"
    r"|(Test|Tests|Spec)\.(java|kt|cs|swift|scala|php)$|_spec\.rb$|\.test\.[^/]+$",
    re.I,
)
ENV = {"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin"}


def run_git(repo: Path, *args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          timeout=timeout, errors="replace", env=ENV)


def has_commit(repo: Path, sha: str) -> bool:
    return run_git(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def ensure_repo(full: str, numbers: list[int], clone_timeout: int) -> Path:
    stem = full.replace("/", "__")
    if (REPOS / stem / "HEAD").exists():
        return REPOS / stem
    dest = LITE / stem
    if not (dest / "HEAD").exists():
        tmp = dest.with_suffix(".tmp")
        subprocess.run(["rm", "-rf", str(tmp)], check=False)
        LITE.mkdir(parents=True, exist_ok=True)
        p = subprocess.run(["git", "clone", "--bare", "--filter=blob:none", "--no-tags", "--quiet",
                            f"https://github.com/{full}.git", str(tmp)],
                           capture_output=True, text=True, timeout=clone_timeout, env=ENV)
        if p.returncode != 0:
            raise RuntimeError(f"clone failed: {p.stderr[-300:]}")
        tmp.rename(dest)
    specs = [f"+refs/pull/{n}/head:refs/pr/{n}" for n in numbers]
    if run_git(dest, "fetch", "--quiet", "--no-tags", "origin", *specs, timeout=1800).returncode:
        for s in specs:
            run_git(dest, "fetch", "--quiet", "--no-tags", "origin", s)
    return dest


def changed_files(repo: Path, fork: str, head: str) -> list[str] | None:
    p = run_git(repo, "diff", "--name-only", "--no-renames", fork, head, timeout=300)
    return p.stdout.split() if p.returncode == 0 else None


def largest_component(members: list[int], pairs: list[dict]) -> list[int]:
    if not pairs:
        return members[:1]
    return build.conflict_components(members, pairs)[0]


def analyse_group(repo: Path, group: dict, min_k: int) -> dict | None:
    comp = set(group["conflict_component"])
    per_pr, comp_rows = [], []
    for p in group["prs"]:  # all members: 03 may build instances from non-largest components
        head = p.get("orig_head") or p["head"]
        files = changed_files(repo, p["fork"], head) if has_commit(repo, head) else None
        tests = [f for f in files or [] if TEST_RE.search(f)]
        per_pr.append({
            "number": p["number"], "agent": p["agent"], "title": p["title"],
            "files_ok": files is not None, "n_files": len(files or []),
            "n_test_files": len(tests), "test_files": tests[:20],
            "touches_src": any(build.is_source(f) and not TEST_RE.search(f) for f in files or []),
            "in_component": p["number"] in comp,
        })
    comp_rows = [r for r in per_pr if r["in_component"]]
    tested = [r["number"] for r in comp_rows if r["n_test_files"] > 0]
    tset = set(tested)
    lock_pairs = [q for q in group["pairs"] if q["n_code_hunks"] > 0]
    src_pairs = build.filter_pairs(group["pairs"], code_only=True)

    def sub(pairs: list[dict]) -> list[dict]:
        return [q for q in pairs if q["a"] in tset and q["b"] in tset]

    return {
        "base": group["base"], "component_size": len(comp),
        "n_files_ok": sum(r["files_ok"] for r in comp_rows),
        "n_tested": len(tested),
        "src_component": len(largest_component(sorted(comp), [q for q in src_pairs
                                                               if q["a"] in comp and q["b"] in comp])),
        "tested_component_lock": largest_component(sorted(tset), sub(lock_pairs)) if tset else [],
        "tested_component_src": largest_component(sorted(tset), sub(src_pairs)) if tset else [],
        "prs": per_pr,
    }


def process(path: Path, min_k: int, clone_timeout: int) -> str:
    mined = json.loads(path.read_text())
    groups = [g for g in mined.get("groups", []) if g["largest_conflict_component"] >= min_k]
    out = OUT / path.name
    t0 = time.time()
    try:
        numbers = sorted({p["number"] for g in groups for p in g["prs"]})
        repo = ensure_repo(mined["full_name"], numbers, clone_timeout)
        res = [r for g in groups if (r := analyse_group(repo, g, min_k))]
        payload = {"full_name": mined["full_name"], "language": mined.get("language"),
                   "stars": mined.get("stars"), "groups": res, "secs": round(time.time() - t0, 1)}
    except Exception as e:  # noqa: BLE001 -- record and continue with other repos
        payload = {"full_name": mined["full_name"], "error": f"{type(e).__name__}: {e}"[:400]}
    out.write_text(json.dumps(payload))
    if "error" in payload:
        return f"ERR  {mined['full_name']} {payload['error'][:120]}"
    best = max(payload["groups"], key=lambda g: len(g["tested_component_src"]), default=None)
    tag = (f"comp={best['component_size']} tested={best['n_tested']} "
           f"tcomp_lock={len(best['tested_component_lock'])} "
           f"tcomp_src={len(best['tested_component_src'])}") if best else "no-groups"
    return f"OK   {mined['full_name']} {tag} {payload['secs']}s"


def summarize(min_k: int) -> None:
    rows = []
    for f in sorted(OUT.glob("*.json")):
        d = json.loads(f.read_text())
        for g in d.get("groups", []):
            rows.append({"repo": d["full_name"], "lang": d.get("language"), **{
                k: g[k] for k in ("component_size", "src_component", "n_tested", "n_files_ok")},
                "tcomp_lock": len(g["tested_component_lock"]),
                "tcomp_src": len(g["tested_component_src"]),
                "all_tested": g["n_tested"] == g["component_size"]})
    errs = sum("error" in json.loads(f.read_text()) for f in OUT.glob("*.json"))
    print(f"repos done={len(list(OUT.glob('*.json')))} errors={errs} groups(comp>={min_k})={len(rows)}")
    for k in (9, 10):
        print(f"--- K={k}")
        for key, label in [("component_size", "conflict comp (02 edges)"),
                           ("src_component", "conflict comp (src-only edges)"),
                           ("tcomp_lock", "test-touching comp (02 edges)"),
                           ("tcomp_src", "test-touching comp (src-only edges)")]:
            hit = [r for r in rows if r[key] >= k]
            print(f"  {label:38s} groups={len(hit):4d} repos={len({r['repo'] for r in hit}):4d}")
        print(f"  {'every PR in comp touches tests':38s} groups="
              f"{sum(r['all_tested'] and r['component_size'] >= k for r in rows):4d}")
    top = sorted(rows, key=lambda r: -r["tcomp_src"])[:30]
    print("--- top by test-touching src-conflict component")
    for r in top:
        print(f"  {r['tcomp_src']:3d} (comp {r['component_size']:2d}, tested {r['n_tested']:2d}) "
              f"{r['lang'] or '?':12s} {r['repo']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-k", type=int, default=9)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--clone-timeout", type=int, default=1800)
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.summary:
        summarize(args.min_k)
        return
    todo = []
    for f in sorted(MINED.glob("*.json")):
        if (OUT / f.name).exists():
            continue
        try:
            d = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        if any(g["largest_conflict_component"] >= args.min_k for g in d.get("groups", [])):
            todo.append(f)
    print(f"todo={len(todo)}", flush=True)
    with ProcessPoolExecutor(args.workers) as ex:
        futs = [ex.submit(process, f, args.min_k, args.clone_timeout) for f in todo]
        for i, fut in enumerate(as_completed(futs), 1):
            print(f"[{i}/{len(todo)}] {fut.result()}", flush=True)


if __name__ == "__main__":
    main()
