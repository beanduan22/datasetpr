"""Stage 1: select candidate repos from AIDev metadata.

A repo is a candidate if it has >= MIN_CONC merged agent PRs that were open
at the same moment (interval clique on [created_at, merged_at]).
Output: data/candidates.jsonl, one line per repo with the merged PR list.
"""
import argparse
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
AIDEV = ROOT / "data" / "aidev"


def max_concurrency(created: pd.Series, merged: pd.Series) -> int:
    events = [(t, 1) for t in created] + [(t, -1) for t in merged]
    events.sort(key=lambda e: (e[0], e[1]))
    cur = best = 0
    for _, delta in events:
        cur += delta
        best = max(best, cur)
    return best


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-conc", type=int, default=5)
    ap.add_argument("--out", default=str(ROOT / "data" / "candidates.jsonl"))
    args = ap.parse_args()

    cols = ["id", "number", "agent", "created_at", "merged_at", "repo_id", "html_url", "title"]
    pr = pd.read_parquet(AIDEV / "pull_request.parquet", columns=cols)
    pr = pr[pr.merged_at.notna()].copy()
    pr["c"] = pd.to_datetime(pr.created_at)
    pr["m"] = pd.to_datetime(pr.merged_at)
    pr["full_name"] = pr.html_url.str.extract(r"github\.com/([^/]+/[^/]+)/pull/")[0]

    repo = pd.read_parquet(AIDEV / "repository.parquet", columns=["id", "stars", "language"])
    repo = repo.rename(columns={"id": "repo_id"})

    rows = []
    for repo_id, g in pr.groupby("repo_id"):
        conc = max_concurrency(g.c, g.m)
        if conc < args.min_conc:
            continue
        prs = [
            {
                "number": int(r.number),
                "agent": r.agent,
                "created_at": r.created_at,
                "merged_at": r.merged_at,
                "title": r.title,
            }
            for r in g.sort_values("m").itertuples()
        ]
        rows.append({"repo_id": int(repo_id), "full_name": g.full_name.iloc[0],
                     "max_conc": conc, "n_merged": len(g), "prs": prs})

    meta = repo.set_index("repo_id").to_dict("index")
    for r in rows:
        info = meta.get(r["repo_id"], {})
        r["stars"] = int(info.get("stars") or 0)
        r["language"] = info.get("language")
    rows.sort(key=lambda r: (-r["max_conc"], -r["stars"]))

    with open(args.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"{len(rows)} candidate repos -> {args.out}")


if __name__ == "__main__":
    main()
