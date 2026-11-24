"""Stage 5: fill missing landing commits from the GitHub API.

Local landing detection (02) relies on "Merge pull request #N" / "(#N)" in the
first-parent history and misses squash/rebase merges with other messages. For
every branch in the given dataset files with landing_commit == null, ask
GET /repos/{repo}/pulls/{n} for merge_commit_sha (the squash / merge / last
rebased commit on the target branch).

Token: read from ~/.aidev_github_token (never printed).
Output: data/landing_api.jsonl, one {"repo", "pr", "merge_commit_sha", "status"} per line
(resumable). 03_build_dataset.py --landing-api applies it.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "landing_api.jsonl"
TOKEN_FILE = Path.home() / ".aidev_github_token"
LOCK = threading.Lock()


def load_token() -> str:
    if not TOKEN_FILE.exists() or not TOKEN_FILE.read_text().strip():
        sys.exit(f"missing token file {TOKEN_FILE}")
    return TOKEN_FILE.read_text().strip()


def get_pr(token: str, repo: str, pr: int) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/pulls/{pr}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "User-Agent": "aidev-conflict-mining", "X-GitHub-Api-Version": "2022-11-28"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                remaining = int(r.headers.get("X-RateLimit-Remaining", "1000"))
                if remaining < 20:
                    wait = int(r.headers.get("X-RateLimit-Reset", "0")) - time.time() + 5
                    print(f"rate limit low, sleeping {int(wait)}s", flush=True)
                    time.sleep(max(wait, 0))
                d = json.load(r)
                return {"repo": repo, "pr": pr, "merge_commit_sha": d.get("merge_commit_sha"),
                        "merged": d.get("merged"), "base_ref": d["base"]["ref"], "status": 200}
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                reset = int(e.headers.get("X-RateLimit-Reset", "0"))
                time.sleep(max(reset - time.time() + 5, 30 * (attempt + 1)))
                continue
            return {"repo": repo, "pr": pr, "merge_commit_sha": None, "status": e.code}
        except (urllib.error.URLError, TimeoutError):
            time.sleep(5 * (attempt + 1))
    return {"repo": repo, "pr": pr, "merge_commit_sha": None, "status": "retries_exhausted"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--datasets", nargs="+",
                    default=["data/dataset_c9_code.jsonl", "data/dataset_c9_full.jsonl"])
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    token = load_token()

    todo = set()
    for f in args.datasets:
        for line in open(ROOT / f):
            x = json.loads(line)
            todo |= {(x["repo"], b["pr_number"]) for b in x["branches"] if not b["landing_commit"]}
    done = set()
    if OUT.exists():
        done = {(r["repo"], r["pr"]) for r in map(json.loads, OUT.open())}
    todo = sorted(todo - done)
    print(f"todo={len(todo)} cached={len(done)}", flush=True)

    with ThreadPoolExecutor(args.workers) as ex, OUT.open("a") as out:
        futs = [ex.submit(get_pr, token, r, n) for r, n in todo]
        for i, fut in enumerate(as_completed(futs), 1):
            res = fut.result()
            with LOCK:
                out.write(json.dumps(res) + "\n")
                out.flush()
            if i % 200 == 0 or i == len(todo):
                print(f"[{i}/{len(todo)}]", flush=True)


if __name__ == "__main__":
    main()
