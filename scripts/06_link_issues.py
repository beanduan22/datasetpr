"""Stage 6: find the issues linked to every PR in the dataset (GitHub GraphQL API).

For each (repo, pr) in the given dataset files:
  * closing_issues   - PullRequest.closingIssuesReferences: issues closed by the PR, from
                       "Fixes #N"-style keywords or a manual link in the Development sidebar
  * mentioned_issues - other "#N", "owner/repo#N" or ".../issues/N" references in the PR
                       body that resolve to an Issue (not a PR)

Token: read from ~/.aidev_github_token (never printed).
Output: data/issues_api.jsonl, one record per PR (resumable).
`--summary` prints coverage per instance subset.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "issues_api.jsonl"
TOKEN_FILE = Path.home() / ".aidev_github_token"
DEFAULT_DATASETS = ["data/dataset_c9_code.jsonl", "data/dataset_c9_full.jsonl"]
API = "https://api.github.com/graphql"
LOCK = threading.Lock()
BATCH = 20
ISSUE_FIELDS = "number url title body state createdAt closedAt repository { nameWithOwner }"
REF_RE = re.compile(
    r"(?:https?://github\.com/(?P<u_repo>[\w.-]+/[\w.-]+)/issues/(?P<u_num>\d+))"
    r"|(?:(?<![\w/])(?P<s_repo>[\w.-]+/[\w.-]+)?#(?P<s_num>\d+)\b)")


def load_token() -> str:
    if not TOKEN_FILE.exists() or not TOKEN_FILE.read_text().strip():
        sys.exit(f"missing token file {TOKEN_FILE}")
    return TOKEN_FILE.read_text().strip()


def graphql(token: str, query: str) -> dict:
    req = urllib.request.Request(
        API, data=json.dumps({"query": query}).encode(),
        headers={"Authorization": f"Bearer {token}", "User-Agent": "aidev-conflict-mining"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                reset = int(e.headers.get("X-RateLimit-Reset", "0") or 0)
                wait = max(reset - time.time() + 5, 30 * (attempt + 1))
                print(f"rate limited ({e.code}), sleeping {int(wait)}s", flush=True)
                time.sleep(wait)
            elif e.code >= 500:
                time.sleep(5 * (attempt + 1))
            else:
                raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("GraphQL request failed after retries")


def split_repo(repo: str) -> tuple[str, str]:
    owner, name = repo.split("/", 1)
    return json.dumps(owner), json.dumps(name)


def pr_query(batch: list[tuple[str, int]]) -> str:
    parts = []
    for i, (repo, pr) in enumerate(batch):
        owner, name = split_repo(repo)
        parts.append(
            f"p{i}: repository(owner: {owner}, name: {name}) {{ pullRequest(number: {pr}) {{ "
            f"body closingIssuesReferences(first: 20) {{ nodes {{ {ISSUE_FIELDS} }} }} }} }}")
    return "{ " + " ".join(parts) + " }"


def ref_query(batch: list[tuple[str, int]]) -> str:
    parts = []
    for i, (repo, num) in enumerate(batch):
        owner, name = split_repo(repo)
        parts.append(
            f"r{i}: repository(owner: {owner}, name: {name}) {{ issueOrPullRequest(number: {num}) "
            f"{{ __typename ... on Issue {{ {ISSUE_FIELDS} }} }} }}")
    return "{ " + " ".join(parts) + " }"


def issue_record(node: dict) -> dict:
    return {"repo": node["repository"]["nameWithOwner"], "number": node["number"],
            "url": node["url"], "title": node["title"], "body": node["body"],
            "state": node["state"], "created_at": node["createdAt"],
            "closed_at": node["closedAt"]}


def body_refs(repo: str, pr: int, body: str) -> list[tuple[str, int]]:
    refs = []
    for m in REF_RE.finditer(body or ""):
        ref_repo = m.group("u_repo") or m.group("s_repo") or repo
        num = int(m.group("u_num") or m.group("s_num"))
        key = (ref_repo, num)
        if key != (repo, pr) and key not in refs:
            refs.append(key)
    return refs[:20]


def resolve_refs(token: str, refs: list[tuple[str, int]]) -> dict[tuple[str, int], dict]:
    found = {}
    for start in range(0, len(refs), BATCH):
        batch = refs[start:start + BATCH]
        data = graphql(token, ref_query(batch)).get("data") or {}
        for i, key in enumerate(batch):
            node = ((data.get(f"r{i}") or {}).get("issueOrPullRequest")) or {}
            if node.get("__typename") == "Issue":
                found[key] = issue_record(node)
    return found


def fetch_batch(token: str, batch: list[tuple[str, int]]) -> list[dict]:
    resp = graphql(token, pr_query(batch))
    data = resp.get("data") or {}
    records, pending = [], {}
    for i, (repo, pr) in enumerate(batch):
        node = (data.get(f"p{i}") or {}).get("pullRequest")
        if node is None:
            records.append({"repo": repo, "pr": pr, "status": "not_found",
                            "closing_issues": [], "mentioned_issues": []})
            continue
        closing = [issue_record(n) for n in node["closingIssuesReferences"]["nodes"]]
        closing_keys = {(c["repo"], c["number"]) for c in closing}
        refs = [r for r in body_refs(repo, pr, node["body"]) if r not in closing_keys]
        pending[(repo, pr)] = refs
        records.append({"repo": repo, "pr": pr, "status": "ok",
                        "closing_issues": closing, "mentioned_issues": []})
    all_refs = sorted({r for refs in pending.values() for r in refs})
    resolved = resolve_refs(token, all_refs) if all_refs else {}
    return [
        {**rec, "mentioned_issues": [resolved[r] for r in pending.get((rec["repo"], rec["pr"]), [])
                                     if r in resolved]}
        for rec in records
    ]


def load_instances(paths: list[str]) -> list[dict]:
    instances = []
    for p in paths:
        with open(ROOT / p) as f:
            instances.extend(json.loads(line) for line in f)
    return instances


def load_done() -> dict[tuple[str, int], dict]:
    if not OUT.exists():
        return {}
    done = {}
    with open(OUT) as f:
        for line in f:
            rec = json.loads(line)
            done[(rec["repo"], rec["pr"])] = rec
    return done


def run(args: argparse.Namespace) -> None:
    token = load_token()
    prs = sorted({(d["repo"], b["pr_number"]) for d in load_instances(args.datasets)
                  for b in d["branches"]})
    done = load_done()
    todo = [p for p in prs if p not in done]
    print(f"{len(prs)} PRs, {len(done)} done, {len(todo)} to fetch", flush=True)
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    n_done = 0
    with ThreadPoolExecutor(args.workers) as ex, open(OUT, "a") as out:
        futs = {ex.submit(fetch_batch, token, b): b for b in batches}
        for fut in as_completed(futs):
            try:
                recs = fut.result()
            except Exception as e:  # noqa: BLE001 - log and continue, batch is retried on rerun
                print(f"batch failed ({futs[fut][0]}...): {e}", flush=True)
                continue
            with LOCK:
                out.writelines(json.dumps(rec) + "\n" for rec in recs)
                out.flush()
            n_done += 1
            if n_done % 20 == 0 or n_done == len(batches):
                print(f"[{n_done}/{len(batches)}]", flush=True)


def has_issue(rec: dict | None, mode: str) -> bool:
    if not rec:
        return False
    if mode == "closing":
        return bool(rec["closing_issues"])
    return bool(rec["closing_issues"] or rec["mentioned_issues"])


def subset_stats(name: str, instances: list[dict], done: dict) -> None:
    prs = [(d["repo"], b["pr_number"]) for d in instances for b in d["branches"]]
    print(f"\n== {name}: {len(instances)} instances, {len(prs)} PRs")
    for mode in ("closing", "any"):
        n_pr = sum(has_issue(done.get(p), mode) for p in prs)
        per_inst = [sum(has_issue(done.get((d["repo"], b["pr_number"])), mode)
                        for b in d["branches"]) for d in instances]
        all_have = sum(c == len(d["branches"]) for c, d in zip(per_inst, instances))
        half = sum(c * 2 >= len(d["branches"]) for c, d in zip(per_inst, instances))
        some = sum(c > 0 for c in per_inst)
        pct = 100 * n_pr / max(len(prs), 1)
        print(f"  [{mode:7}] PRs with issue {n_pr} ({pct:.1f}%) | instances: every PR {all_have}, "
              f">=half {half}, >=1 PR {some}")


def summary(args: argparse.Namespace) -> None:
    done = load_done()
    status = Counter(r["status"] for r in done.values())
    print(f"records: {len(done)} {dict(status)}")
    for path in args.datasets:
        inst = load_instances([path])
        rel = [d for d in inst if d.get("gold_all_landed") and d.get("all_target_default")]
        rel_t = [d for d in rel if d.get("all_branches_touch_tests")]
        print(f"\n######## {path}")
        subset_stats("all", inst, done)
        subset_stats("reliable gold", rel, done)
        subset_stats("reliable + every PR touches tests", rel_t, done)
        subset_stats("  └ Python", [d for d in rel_t if d["language"] == "Python"], done)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--datasets", nargs="+", default=DEFAULT_DATASETS)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()
    summary(args) if args.summary else run(args)


if __name__ == "__main__":
    main()
