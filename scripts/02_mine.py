"""Stage 2: replay merges locally to find N-way conflicts among agent PRs.

For every candidate repo:
  1. bare-clone the repo and fetch refs/pull/<N>/head for all merged agent PRs
  2. fork point F_i = merge-base(head_i, main just before PR i merged)
  3. group PRs by identical fork point  -> "same-base" groups (size >= MIN_GROUP)
  4. for every pair in a group run `git merge-tree --write-tree` and record
     conflicted files / conflict hunks / conflict types
  5. record each PR's landing commit on main (ground-truth after merge)

Output: data/mined/<owner>__<repo>.json  (resumable; existing files skipped)
"""
from __future__ import annotations

import argparse
import bisect
import calendar
import itertools
import json
import re
import subprocess
import sys
import time
import traceback
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPOS = ROOT / "data" / "repos"
OUT = ROOT / "data" / "mined"

NOISE_RE = re.compile(
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|bun\.lockb?|Cargo\.lock|"
    r"poetry\.lock|uv\.lock|Pipfile\.lock|go\.sum|composer\.lock|Gemfile\.lock|"
    r"packages\.lock\.json|.*\.min\.js|.*\.snap)$"
)
CONFLICT_MSG_RE = re.compile(r"^CONFLICT \(([^)]+)\)", re.M)


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str, timeout: int = 600, ok_codes=(0,), input_: str | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        timeout=timeout, input=input_, errors="replace",
    )
    if proc.returncode not in ok_codes:
        raise GitError(f"git {' '.join(args[:3])} -> {proc.returncode}: {proc.stderr[-400:]}")
    return proc.returncode, proc.stdout


def clone(full_name: str, clone_timeout: int) -> Path:
    dest = REPOS / full_name.replace("/", "__")
    if (dest / "HEAD").exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    subprocess.run(["rm", "-rf", str(tmp)], check=False)
    proc = subprocess.run(
        ["git", "clone", "--bare", "--no-tags", "--quiet",
         f"https://github.com/{full_name}.git", str(tmp)],
        capture_output=True, text=True, timeout=clone_timeout,
        env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin"},
    )
    if proc.returncode != 0:
        raise GitError(f"clone failed: {proc.stderr[-300:]}")
    tmp.rename(dest)
    return dest


def fetch_pr_heads(repo: Path, numbers: list[int]) -> None:
    for i in range(0, len(numbers), 300):
        specs = [f"+refs/pull/{n}/head:refs/pr/{n}" for n in numbers[i:i + 300]]
        # missing refs make the whole fetch fail -> fall back to one by one
        try:
            git(repo, "fetch", "--quiet", "--no-tags", "origin", *specs, timeout=1800)
        except GitError:
            for s in specs:
                try:
                    git(repo, "fetch", "--quiet", "--no-tags", "origin", s, timeout=600)
                except GitError:
                    pass


def resolve_refs(repo: Path, numbers: list[int]) -> dict[int, str]:
    _, out = git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/pr/")
    heads = {}
    for line in out.split("\n"):
        ref, sha = line.split()
        heads[int(ref.rsplit("/", 1)[1])] = sha
    return {n: heads[n] for n in numbers if n in heads}


def first_parent_history(repo: Path) -> tuple[list[int], list[str], list[str]]:
    _, out = git(repo, "log", "--first-parent", "--format=%ct %H %s", "HEAD", timeout=600)
    rows = []
    for line in out.split("\n"):
        if not line:
            continue
        ct, sha, *subj = line.split(" ", 2)
        rows.append((int(ct), sha, subj[0] if subj else ""))
    rows.sort(key=lambda r: r[0])
    return [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows]


def iso_to_ts(s: str) -> int:
    return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))


PR_REF_RE = re.compile(r"(?:Merge pull request #(\d+)\b|\(#(\d+)\)\s*$)")


def landing_index(repo: Path) -> dict[int, list[tuple[int, str, str]]]:
    """Map PR number -> [(commit_ts, sha, first_parent)] over all branches.

    Matches GitHub merge commits ("Merge pull request #N") and squash commits
    whose subject ends with "(#N)".
    """
    _, out = git(repo, "log", "--branches", "--format=%ct %H %P%x00%s", timeout=1200)
    idx: dict[int, list] = defaultdict(list)
    for line in out.split("\n"):
        if not line:
            continue
        head, _, subj = line.partition("\x00")
        m = PR_REF_RE.search(subj)
        if not m:
            continue
        ct, sha, *parents = head.split()
        if not parents:
            continue
        idx[int(m.group(1) or m.group(2))].append((int(ct), sha, parents[0]))
    return idx


def pick_landing(cands: list, merged_ts: int) -> tuple[str, str] | None:
    """Closest landing commit to the recorded merge time (within 1 day)."""
    best = min(cands, key=lambda c: abs(c[0] - merged_ts), default=None)
    if best is None or abs(best[0] - merged_ts) > 86400:
        return None
    return best[1], best[2]


def merge_pair(repo: Path, a: str, b: str) -> dict:
    code, out = git(repo, "merge-tree", "--write-tree", "--name-only", a, b,
                    ok_codes=(0, 1), timeout=300)
    if code == 0:
        return {"conflict": False}
    parts = out.split("\n\n", 1)
    lines = parts[0].splitlines()
    tree, files = lines[0], sorted(set(lines[1:]))
    msgs = parts[1] if len(parts) > 1 else ""
    types = sorted(set(CONFLICT_MSG_RE.findall(msgs)))
    hunks = {}
    for f in files:
        try:
            _, blob = git(repo, "cat-file", "-p", f"{tree}:{f}", timeout=60)
            hunks[f] = sum(1 for ln in blob.splitlines() if ln.startswith("<<<<<<<"))
        except GitError:
            hunks[f] = 0  # deleted / binary / directory conflict
    code_files = [f for f in files if not NOISE_RE.search(f)]
    return {
        "conflict": True,
        "files": files,
        "code_files": code_files,
        "hunks": hunks,
        "n_hunks": sum(hunks.values()),
        "n_code_hunks": sum(hunks[f] for f in code_files),
        "types": types,
    }


def original_branch(repo: Path, head: str, before: str) -> dict | None:
    """Recover the agent's original work, undoing later "sync with base" merges.

    Walk the first-parent chain of `head` that is not on the target branch:
      fork      = parent of the oldest such commit (original fork point)
      orig_head = last commit before the first merge commit on that chain
    Merge commits on the chain are sync merges whose result is a real,
    human/agent-authored conflict resolution.
    """
    _, out = git(repo, "rev-list", "--first-parent", "--reverse", "--parents",
                 head, f"^{before}", timeout=120)
    chain = [ln.split() for ln in out.split("\n") if ln.strip()]
    if not chain:
        return None  # head already contained in target branch (fast-forward)
    fork = chain[0][1] if len(chain[0]) > 1 else None
    if fork is None:
        return None
    first_merge = next((i for i, c in enumerate(chain) if len(c) > 2), None)
    if first_merge == 0:
        return None
    work = chain if first_merge is None else chain[:first_merge]
    sync = [c[0] for c in chain if len(c) > 2]
    _, ft = git(repo, "show", "-s", "--format=%ct", fork, timeout=60)
    return {"fork": fork, "orig_head": work[-1][0], "n_commits": len(work),
            "n_commits_total": len(chain), "sync_merges": sync,
            "fork_ts": int(ft.strip())}


def components(nodes: list[int], edges: list[tuple[int, int]]) -> list[list[int]]:
    parent = {n: n for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        parent[find(a)] = find(b)
    comp = defaultdict(list)
    for n in nodes:
        comp[find(n)].append(n)
    return sorted(comp.values(), key=len, reverse=True)


def mine_repo(cand: dict, min_group: int, max_group: int, clone_timeout: int) -> dict:
    full = cand["full_name"]
    t0 = time.time()
    repo = clone(full, clone_timeout)
    prs = {p["number"]: p for p in cand["prs"]}
    fetch_pr_heads(repo, sorted(prs))
    heads = resolve_refs(repo, sorted(prs))
    cts, shas, _ = first_parent_history(repo)
    lidx = landing_index(repo)

    info = {}
    for n, head in heads.items():
        p = prs[n]
        mts = iso_to_ts(p["merged_at"])
        landing = pick_landing(lidx.get(n, []), mts)
        if landing:  # target branch state right before the merge
            landing_sha, before = landing
            method = "landing_commit"
        else:  # fall back to default branch at merge time (rebase merges)
            k = bisect.bisect_left(cts, mts) - 1
            if k < 0:
                continue
            landing_sha, before, method = None, shas[k], "default_branch_time"
        orig = original_branch(repo, head, before)
        if orig is None:
            continue
        info[n] = {**p, "head": head, "landing": landing_sha, "before_merge": before,
                   "fork_method": method, **orig}

    by_fork = defaultdict(list)
    for n, d in info.items():
        by_fork[d["fork"]].append(n)

    groups = []
    for fork, members in by_fork.items():
        if len(members) < min_group:
            continue
        members = sorted(members, key=lambda n: info[n]["merged_at"])[:max_group]
        pairs = []
        for a, b in itertools.combinations(members, 2):
            try:
                r = merge_pair(repo, info[a]["orig_head"], info[b]["orig_head"])
            except (GitError, subprocess.TimeoutExpired) as e:
                r = {"conflict": None, "error": str(e)[:200]}
            if r.get("conflict"):
                pairs.append({"a": a, "b": b, **r})
        code_edges = [(p["a"], p["b"]) for p in pairs if p["n_code_hunks"] > 0]
        comps = components(members, code_edges)
        groups.append({
            "base": fork,
            "size": len(members),
            "prs": [info[n] for n in members],
            "agents": sorted({info[n]["agent"] for n in members}),
            "n_conflict_pairs": len(pairs),
            "n_code_conflict_pairs": len(code_edges),
            "total_code_hunks": sum(p["n_code_hunks"] for p in pairs),
            "largest_conflict_component": len(comps[0]) if code_edges else 1,
            "conflict_component": comps[0] if code_edges else [],
            "pairs": pairs,
        })
    groups.sort(key=lambda g: (-g["largest_conflict_component"], -g["total_code_hunks"]))
    return {
        "full_name": full, "stars": cand.get("stars"), "language": cand.get("language"),
        "n_prs": len(prs), "n_heads": len(heads), "n_fork_resolved": len(info),
        "n_groups": len(groups), "groups": groups, "secs": round(time.time() - t0, 1),
    }


def worker(cand: dict, min_group: int, max_group: int, clone_timeout: int) -> str:
    out = OUT / (cand["full_name"].replace("/", "__") + ".json")
    try:
        res = mine_repo(cand, min_group, max_group, clone_timeout)
    except Exception as e:  # record failure, keep the batch going
        res = {"full_name": cand["full_name"], "error": f"{type(e).__name__}: {e}"[:500],
               "trace": traceback.format_exc()[-1500:]}
    out.write_text(json.dumps(res))
    if "error" in res:
        return f"ERR  {cand['full_name']}: {res['error'][:120]}"
    best = res["groups"][0] if res["groups"] else None
    tag = (f"groups={res['n_groups']} best_comp={best['largest_conflict_component']} "
           f"hunks={best['total_code_hunks']}") if best else "groups=0"
    return f"OK   {cand['full_name']} heads={res['n_heads']}/{res['n_prs']} {tag} {res['secs']}s"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", default=str(ROOT / "data" / "candidates.jsonl"))
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--min-group", type=int, default=5)
    ap.add_argument("--max-group", type=int, default=40)
    ap.add_argument("--max-prs", type=int, default=3000, help="skip repos with more merged PRs")
    ap.add_argument("--clone-timeout", type=int, default=1200)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    cands = [json.loads(l) for l in open(args.candidates)]
    cands = [c for c in cands if c["n_merged"] <= args.max_prs][args.start:args.start + args.limit]
    todo = [c for c in cands if not (OUT / (c["full_name"].replace("/", "__") + ".json")).exists()]
    print(f"{len(todo)} repos to mine ({len(cands) - len(todo)} cached)", flush=True)
    with ProcessPoolExecutor(args.workers) as ex:
        futs = [ex.submit(worker, c, args.min_group, args.max_group, args.clone_timeout) for c in todo]
        for i, f in enumerate(as_completed(futs), 1):
            print(f"[{i}/{len(todo)}] {f.result()}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
