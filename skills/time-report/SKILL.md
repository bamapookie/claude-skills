---
name: time-report
description:
  Build a time-on-project report and per-day timesheet from session timelines, git commits, and PR/issue history. Use
  when the user asks how much time was spent, for a timesheet or time metrics, as part of a sprint review/sprint-end
  ritual, or on a timer/routine. Consumes the tracking branch written by the session-timelog skill.
source: https://github.com/BinaryInfinityDev/claude-skills/blob/main/skills/time-report/SKILL.md
---

# Time Report

Turn timestamped activity into two committed artifacts:

- **`metrics/time-report.md`** — headline hours, per-ISO-week and per-day tables (UTC-bucketed), method and caveats.
- **`metrics/timesheet.md`** — a human timesheet: one row per work block in a local timezone, with per-day and per-week
  active totals.

This skill is **project-agnostic**; per-project settings come from `.claude/timelog.yaml`.

---

## Configuration

| Key (`.claude/timelog.yaml`) | Default                    | Purpose                                       |
| ---------------------------- | -------------------------- | --------------------------------------------- |
| `branch`                     | `metrics/session-timelogs` | Tracking branch to consume timelines from     |
| `timezone`                   | `UTC`                      | Timesheet timezone (the report is always UTC) |
| `out_dir`                    | `metrics/`                 | Where the two artifacts are written           |

## Event sources — pool all that exist

1. **Session timelines** — the tracking branch written by `session-timelog`
   (`git archive origin/<branch> timelines | tar -x` into a temp dir), plus any consolidated `session-timeline.csv[.gz]`
   extract already committed. Timeline events capture the reading/typing/tool time _between_ commits, turning the tight
   clustering into a genuine engagement measure rather than a floor.
2. **Git commits** — author timestamps from **all branches** (`git fetch` everything first; author dates survive
   rebases, committer dates don't). Re-collect from current refs at report time: branches pushed after an earlier
   collection are a real, easy-to-miss gap. `git log --all --format='%H|%aI|%cI|%an|%ae' > commits.psv`
3. **GitHub PRs and issues** — created/merged/closed timestamps, as `number|created|merged|closed|user|title`
   (`prs.psv`) and `number|created|closed|user|title` (`issues.psv`). Use `gh` where available; in cloud sessions use
   the GitHub MCP list tools. Page in _ascending_ created order until exhausted, then **verify the tail**: the highest
   item number fetched must match the repo's actual newest (a lost final page silently truncates the range).

## Build the artifacts

```bash
python3 references/build_time_report.py \
  --timelines <dir with *.timeline.jsonl and/or session-timeline.csv[.gz]> \
  --commits commits.psv [--prs prs.psv] [--issues issues.psv] \
  --tz <timezone> --out <out_dir>
```

The script implements the method; when describing it in prose, keep these invariants intact (each encodes a real review
finding):

- **Two clusterings bound the estimate**: 10-minute gap + 3-minute lead-in per burst (engaged time — the headline
  number) and 30-minute gap + 10-minute lead-in (work windows including short CI/review waits).
- **Split bursts at midnight** of whichever timezone a table buckets by, crediting each day only its own portion. This
  makes per-day `window ≥ active` hold **by construction** (asserted per date); crediting whole bursts to their start
  date produces impossible-looking inversions on overnight work.
- **Subagent timelines overlap their parent session**; pooling + clustering merges overlaps automatically — never sum
  per-file hours.
- **Disclose the conventions**: label every table with its bucketing timezone; omitted dates are zero-activity, not data
  gaps; mark the final week partial with the dataset end (in both local and UTC terms when the timesheet uses local
  time); a block crossing midnight splits at the boundary (the prior row ends `00:00` and it continues on the next row), while a block that *ends* exactly at midnight is credited wholly to the prior day; displayed block starts include the lead-in, so
  visible gaps read shorter than the underlying silence; week/grand totals use unrounded durations and may not equal the
  sum of rounded day figures; headline numbers carry the same precision as the tables they summarize.
- **State coverage**: which days have timeline coverage versus git/GitHub signal only (those undercount), and that
  report-generation activity is itself in the data.

Commit the artifacts on a normal branch and open a normal PR — it is only the _tracking_ branch that never gets one.

## Consuming and clearing the tracking branch

After the report PR merges:

1. Optionally fold the consumed timeline files into a consolidated CSV so nothing is lost, then
2. reset the tracking branch to its init commit (or delete it) to keep it small. This discards data — **confirm with the
   user first**, and never clear before the report referencing those timelines has actually merged.

## Trigger phrases

- "how much time have I spent", "build the timesheet", "time report", "time metrics", "/time-report"
- **Sprint review**: run as a sprint-end step so each sprint's record includes its hours; snapshot the report with the
  sprint docs if the project keeps sprint snapshots.
- **On a timer**: create a scheduled routine (cron trigger / fresh-session routine) whose prompt is "run /time-report
  and open a draft PR with the refreshed artifacts".
