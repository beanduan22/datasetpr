# datasetpr — N-way merge conflicts among concurrent AI-agent PRs

Instances where **≥ 5 merged PRs authored by AI coding agents** (Copilot, Claude Code,
OpenAI Codex, Cursor, Google Jules, Devin) fork from **the same base commit** and form
**one connected component in the pairwise merge-conflict graph**. Mined from the
[AIDev](https://huggingface.co/datasets/hao-li/AIDev) dataset by replaying merges locally
with `git merge-tree`.

## Files

| File | Description |
|---|---|
| `data/dataset_full.jsonl` | 148 instances (112 repos), conflicts in any file (lock files ignored) |
| `data/dataset_full_code.jsonl` | 89 instances (68 repos), conflicts in source files only |
| `data/dataset.jsonl`, `data/dataset_code.jsonl` | pilot run on the first ~300 candidate repos |
| `data/summary*.json` | aggregate statistics for each dataset file |
| `data/candidates.jsonl` | 1331 candidate repos (≥ 5 agent PRs open concurrently) |
| `data/mined/*.json` | per-repo raw mining output (same-base groups, pairwise conflicts, landing commits) |
| `scripts/` | pipeline that produces the above |

## Instance schema

```
instance_id, repo, language, stars, base_commit, k, agents{agent: count},
branches[ {pr_number, pr_url, title, agent, created_at, merged_at,
           orig_head, final_head, n_commits, sync_merges, landing_commit} ],
conflict_pairs, n_conflict_pairs, n_conflict_hunks, conflict_files,
gold_final_commit, gold_all_landed
```

- `base_commit`: shared fork point of all PRs in the instance
- `orig_head`: PR head as originally authored; `final_head`: PR head at merge time
- `landing_commit`: commit on the target branch where the PR landed
- `gold_final_commit`: landing commit of the last-merged PR (resolved ground truth)
- `gold_all_landed`: whether every PR's landing commit was located

## Statistics (`dataset_full` / `dataset_full_code`)

| | full | code-only |
|---|---|---|
| repos mined | 1278 | 1278 |
| same-base groups (≥5) | 1742 | 1742 |
| groups with any conflict | 516 | 516 |
| instances | 148 | 89 |
| k median / max | 6 / 40 | 6 / 39 |
| instances with k ≥ 10 | 39 | 27 |
| conflict hunks median | 28 | 25 |
| conflict files median | 3 | 4 |
| `gold_all_landed` | 80 | 37 |

Note: hunk/file counts have extreme outliers (max 91,962 hunks); prefer medians.

## Reproduce

```bash
# place AIDev parquet files in data/aidev/
python scripts/01_candidates.py --min-conc 5
python scripts/02_mine.py --start 0 --limit 2000 --workers 16   # clones into data/repos/ (~110 GB)
python scripts/03_build_dataset.py --out dataset_full.jsonl
python scripts/03_build_dataset.py --code-only --out dataset_full_code.jsonl
```
