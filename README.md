# datasetpr — N-way merge conflicts among concurrent AI-agent PRs

Instances where **≥ 5 merged PRs authored by AI coding agents** (Copilot, Claude Code,
OpenAI Codex, Cursor, Google Jules, Devin) fork from **the same base commit** and form
**one connected component in the pairwise merge-conflict graph**. Mined from the
[AIDev](https://huggingface.co/datasets/hao-li/AIDev) dataset by replaying merges locally
with `git merge-tree`.

## Files

| File | Description |
|---|---|
| **`data/dataset_c9_code.jsonl`** | **536 instances (357 repos), k ≥ 9, conflicts in source files only — main release** |
| `data/dataset_c9_full.jsonl` | 835 instances (506 repos), k ≥ 9, conflicts in any file (lock files ignored) |
| `data/summary_c9_*.json` | aggregate statistics for the two k ≥ 9 files |
| `data/issues_api.jsonl` | issues linked to each of the 12583 PRs in the k ≥ 9 files (stage 6) |
| `data/candidates_all_c9.jsonl.gz` | gzip-compressed (raw file exceeds GitHub's 100 MB limit; `gunzip -k` before use): 3553 extra candidate repos from the full AIDev table (≥ 9 agent PRs open concurrently, disjoint from `candidates.jsonl`) |
| `data/test_filter/*.json` | per-repo files changed by every PR in groups with a ≥ 9 conflict component (stage 4) |
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

The `dataset_c9_*` files (built with `--tests`) add:

- per branch `test_files` (≤ 20 shown) and `touches_tests`: test files changed between
  `base_commit` and `orig_head`, matched by path (`tests/`, `test_*.py`, `*.test.ts`,
  `*_test.go`, `*Test.java`, `Foo.Tests/`, …)
- per instance `n_branches_with_tests`, `all_branches_touch_tests`

`touches_tests` is a cheap SWE-bench-style pre-filter only: it does **not** mean a
FAIL_TO_PASS test exists — that still requires building an environment and running tests.

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

## Statistics, k ≥ 9 (`dataset_c9_code` / `dataset_c9_full`)

Mined from both candidate files (1331 + 3553 repos). 4245 repos mined successfully;
622 failed, 573 of them because the repo is gone from GitHub (HTTP 404).

| | code-only | full |
|---|---|---|
| instances (k ≥ 9) | 536 | 835 |
| repos | 357 | 506 |
| k ≥ 10 / ≥ 15 / ≥ 20 | 413 / 177 / 98 | 662 / 280 / 158 |
| k median / max | 12 / 40 | 12 / 40 |
| conflict hunks median | 107 | 97 |
| conflict files median | 7 | 7 |
| `gold_all_landed` | 382 | 606 |
| multi-agent instances | 21 | 27 |
| **every PR touches tests** | **163 (120 repos)** | **215 (134 repos)** |
| └ of which k ≥ 10 / ≥ 15 / ≥ 20 | 129 / 61 / 37 | 166 / 84 / 52 |

Languages of the 163 code-only instances where every PR touches tests: Python 55,
TypeScript 47, Go 11, C# 9, Rust 8, JavaScript 6, C/Kotlin/Dart 4, other 15.

**Gold / target-branch caveat.** Landing commits are first located in the default branch's
first-parent history; the 3384 PRs still missing were looked up via the GitHub API
(`scripts/05_fill_landing.py` → `data/landing_api.jsonl`, `data/default_branch.json`).
Only 546 of them were merged into the default branch (squash/rebase merges, now filled with
`landing_source = "github_api"`); **2833 were merged into another branch** (`develop`,
`codex/…`, `feature/…`). For those, stage 2 derived the fork point from the default branch at
merge time, so their "same base" grouping is unreliable (typically PRs stacked onto one
feature branch) and they keep `landing_commit = null` with `target_branch` set.

**Use instances with `gold_all_landed && all_target_default` for evaluation:**

| | code-only | full |
|---|---|---|
| reliable instances | **408** (281 repos) | 642 (413 repos) |
| └ k ≥ 10 / ≥ 15 / ≥ 20 | 299 / 121 / 64 | 495 / 190 / 97 |
| └ every PR touches tests | **84** (61 with k ≥ 10) | 96 (68 with k ≥ 10) |

Extra fields with `--landing-api`: per branch `landing_source` (`local` / `github_api` /
null) and `target_branch` (`<default>` or the branch name); per instance `all_target_default`;
summary `reliable_gold`.

k is capped at 40 because `02_mine.py` keeps at most 40 PRs per same-base group
(`--max-group`); k = 40 instances may be larger in reality.

## Linked issues (`data/issues_api.jsonl`)

One record per PR: `repo, pr, status (ok / not_found), closing_issues[], mentioned_issues[]`;
each issue has `repo, number, url, title, body, state, created_at, closed_at`.

- `closing_issues`: `PullRequest.closingIssuesReferences` (`Fixes #N` keywords or a manual
  link in the Development sidebar)
- `mentioned_issues`: other `#N` / `owner/repo#N` / `.../issues/N` references in the PR body
  that resolve to an Issue (references to PRs are dropped)

Issues are **extra context, not a filter**: the task description of each branch is the PR
title + body (from AIDev `all_pull_request.parquet`), with linked issue text appended when
present. Very few agent PRs link an issue:

| agent | PRs with a closing issue (`dataset_c9_code`) |
|---|---|
| OpenAI Codex | 2 / 4743 (0%) |
| Devin | 0 / 67 |
| Google Jules | 35 / 559 (6%) |
| Cursor | 11 / 85 (13%) |
| Copilot | 304 / 1463 (21%) |
| Claude Code | 266 / 1143 (23%) |

| `dataset_c9_code` subset | instances | instances with ≥ 1 PR linking an issue | every PR linking an issue |
|---|---|---|---|
| all k ≥ 9 | 536 | 105 | 47 |
| reliable gold | 408 | 47 | 28 |
| reliable + every PR touches tests | 84 | 25 | 12 |
| └ Python | 24 | 8 | 1 |

PR description quality: all 8060 PRs have a body in AIDev (median ~585 chars); 87 are empty or
< 100 chars. Requiring every PR body ≥ 100 chars keeps 512 / 399 / 81 / 22 of the subsets above.

## Reproduce

```bash
# place AIDev parquet files in data/aidev/
python scripts/01_candidates.py --min-conc 5
python scripts/02_mine.py --start 0 --limit 2000 --workers 16   # clones into data/repos/ (~110 GB)
python scripts/03_build_dataset.py --out dataset_full.jsonl
python scripts/03_build_dataset.py --code-only --out dataset_full_code.jsonl

# k >= 9 extension on the full AIDev table
python scripts/01_candidates.py --table all --min-conc 9 --exclude data/candidates.jsonl \
    --out data/candidates_all_c9.jsonl
python scripts/02_mine.py --candidates data/candidates_all_c9.jsonl --limit 100000 --workers 24
python scripts/04_test_filter.py --workers 8          # needs clones from stage 2
python scripts/04_test_filter.py --summary
python scripts/05_fill_landing.py                     # GitHub API; token in ~/.aidev_github_token
python scripts/03_build_dataset.py --code-only --min-k 9 --tests --landing-api --out dataset_c9_code.jsonl
python scripts/03_build_dataset.py --min-k 9 --tests --landing-api --out dataset_c9_full.jsonl
python scripts/06_link_issues.py                      # GitHub GraphQL; same token
python scripts/06_link_issues.py --summary
```
