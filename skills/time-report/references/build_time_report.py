#!/usr/bin/env python3
"""Build time-report.md and timesheet.md from pooled activity events.

Sources:
  --timelines DIR   directory containing *.timeline.jsonl (from the session-timelog
                    tracking branch) and/or session-timeline.csv[.gz] (a consolidated
                    extract). Subagent files overlap their parent; pooling handles it.
  --commits PSV     git commits:  hash|author_iso|committer_iso|name|email
                    (git log --all --format='%H|%aI|%cI|%an|%ae')
  --prs PSV         optional:     number|created|merged|closed|user|title
  --issues PSV      optional:     number|created|closed|user|title

Method (all invariants asserted, not just documented):
  * bursts at 10-min gap (+3-min lead-in) = engaged/active time (headline)
  * bursts at 30-min gap (+10-min lead-in) = work windows
  * bursts split at midnight of the bucketing timezone; per-day 30min >= 10min holds
    by construction and is asserted for every date
"""
import argparse, csv, gzip, json, sys
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

H = lambda td: td.total_seconds() / 3600


def parse_ts(ts):
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        # An offset-less timestamp is treated as UTC. (astimezone on a naive datetime would
        # silently assume the machine's local zone — host-dependent, worse than an error.)
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_events(args):
    tx, gh = [], []
    tdir = Path(args.timelines) if args.timelines else None
    if tdir and tdir.exists():
        for p in tdir.rglob("*.timeline.jsonl"):
            for line in p.open(encoding="utf-8", errors="replace"):
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(o, dict) and o.get("timestamp"):
                    try:
                        tx.append(parse_ts(o["timestamp"]))
                    except ValueError:
                        continue
        for p in list(tdir.rglob("session-timeline.csv")) + list(tdir.rglob("session-timeline.csv.gz")):
            opener = gzip.open if p.suffix == ".gz" else open
            with opener(p, "rt", encoding="utf-8", errors="replace") as f:
                for r in csv.DictReader(f):
                    try:
                        tx.append(parse_ts(r["timestamp"]))
                    except (ValueError, KeyError, TypeError):
                        continue
    if args.commits:
        for line in open(args.commits, encoding="utf-8", errors="replace"):
            parts = line.rstrip("\n").split("|")
            if len(parts) > 1 and parts[1]:
                try:
                    gh.append(parse_ts(parts[1]))
                except ValueError:
                    continue
    if args.prs:
        for line in open(args.prs, encoding="utf-8", errors="replace"):
            p = line.rstrip("\n").split("|", 5)
            if len(p) < 4:
                continue
            for x in (p[1], p[2], p[3]):
                if x:
                    try:
                        gh.append(parse_ts(x))
                    except ValueError:
                        continue
    if args.issues:
        for line in open(args.issues, encoding="utf-8", errors="replace"):
            p = line.rstrip("\n").split("|", 4)
            if len(p) < 3:
                continue
            for x in (p[1], p[2]):
                if x:
                    try:
                        gh.append(parse_ts(x))
                    except ValueError:
                        continue
    return sorted(set(tx)), sorted(gh)


def bursts(evts, gap_min, lead_min):
    gap, lead = timedelta(minutes=gap_min), timedelta(minutes=lead_min)
    out, start, prev = [], evts[0], evts[0]
    for t in evts[1:] + [None]:
        if t is None or t - prev > gap:
            out.append((start - lead, prev))
            if t is None:
                break
            start = t
        prev = t
    return out


def split_by_day(bs, tz):
    db = defaultdict(list)
    for s, e in bs:
        s, e = s.astimezone(tz), e.astimezone(tz)
        while s.date() < e.date():
            nxt = datetime.combine(s.date() + timedelta(days=1), time(0), tzinfo=tz)
            db[s.date()].append((s, nxt))
            s = nxt
        if s < e:  # an end of exactly midnight belongs wholly to the prior day: no zero-length tail
            db[s.date()].append((s, e))
    return db


def daily_totals(db):
    return {d: sum(((e - s) for s, e in v), timedelta()) for d, v in db.items()}


def weekly(days):
    wk = defaultdict(timedelta)
    for d, v in days.items():
        wk[d.strftime("%G-W%V")] += v
    return wk


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timelines")
    ap.add_argument("--commits")
    ap.add_argument("--prs")
    ap.add_argument("--issues")
    ap.add_argument("--tz", default="UTC", help="timesheet timezone (report is always UTC)")
    ap.add_argument("--out", default="metrics")
    ap.add_argument("--gap-active", type=float, default=10)
    ap.add_argument("--lead-active", type=float, default=3)
    ap.add_argument("--gap-window", type=float, default=30)
    ap.add_argument("--lead-window", type=float, default=10)
    args = ap.parse_args()

    tx, gh = load_events(args)
    allev = sorted(tx + gh)
    if not allev:
        sys.exit("no events found")
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"pool: {len(allev)} events ({len(tx)} timeline, {len(gh)} git/GitHub), "
          f"{allev[0]:%Y-%m-%d} .. {allev[-1]:%Y-%m-%d} UTC")

    b_act = bursts(allev, args.gap_active, args.lead_active)
    b_win = bursts(allev, args.gap_window, args.lead_window)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # ---------------- report (UTC) ----------------
    utc = timezone.utc
    d_act, d_win = daily_totals(split_by_day(b_act, utc)), daily_totals(split_by_day(b_win, utc))
    bad = [d for d in d_act if d_act[d] > d_win.get(d, timedelta()) + timedelta(seconds=1)]
    if bad:
        sys.exit(f"invariant violated (window < active) on {bad}")
    t_act, t_win = sum(d_act.values(), timedelta()), sum(d_win.values(), timedelta())
    tdays = {t.astimezone(utc).date() for t in tx}
    first, last = min(d_win), max(d_win)
    ncal = (last - first).days + 1
    uncov = sorted(d.isoformat() for d in set(d_win) - tdays)
    wa, ww = weekly(d_act), weekly(d_win)

    L = [
        "# Time-on-project metrics",
        "",
        f"Generated {gen} from session timelines, git commits (all branches), and the",
        "GitHub PR/issue history, pooled and clustered into activity bursts.",
        "",
        "## Method",
        "All timestamped events are pooled; consecutive events closer than a gap threshold",
        f"form one burst, whose duration is last−first plus a lead-in ({args.lead_active:g} min at the",
        f"{args.gap_active:g}-minute gap, {args.lead_window:g} min at the {args.gap_window:g}-minute gap). Bursts crossing UTC",
        "midnight are split at the boundary, each day credited only its own portion, so the",
        "per-day columns satisfy window ≥ active by construction (asserted per date).",
        "Timeline events capture activity *between* commits, so the tight clustering is a",
        "genuine engagement measure, not a floor.",
        "",
        "## Headline numbers",
        "",
        "| Measure | Hours |",
        "| --- | --- |",
        f"| Active time ({args.gap_active:g}-min gap, recommended) | **{H(t_act):.1f} h** |",
        f"| Work windows ({args.gap_window:g}-min gap, incl. short waits) | {H(t_win):.1f} h |",
        f"| Active days (UTC calendar days) | {len(d_win)} of {ncal} ({first} → {last}) |",
        f"| Average per active day (active basis) | {H(t_act) / len(d_act):.1f} h |",
        "",
    ]
    if uncov:
        L += [f"Timeline coverage exists for {len(set(d_win) & tdays)} of {len(d_win)} working days; days with only",
              f"git/GitHub signal ({', '.join(uncov)}) are undercounted.", ""]
    L += ["## Per ISO week (UTC)", "", "| Week | Active (h) | Windows (h) |", "| --- | --- | --- |"]
    for w in sorted(ww):
        L.append(f"| {w} | {H(wa.get(w, timedelta())):.1f} | {H(ww[w]):.1f} |")
    L += ["", "## Per day (UTC)", "",
          "Dates with no recorded events are omitted: zero-activity days, not data gaps.", "",
          "| Date | Active (h) | Windows (h) | Timeline? | Bar (active) |", "| --- | --- | --- | --- | --- |"]
    for d in sorted(d_win):
        a = H(d_act.get(d, timedelta()))
        L.append(f"| {d} | {a:.1f} | {H(d_win[d]):.1f} | {'yes' if d in tdays else '—'} | {'█' * max(1, round(a))} |")
    L += ["", "## Caveats",
          "- Timeline timestamps include agent runtime under supervision; heavy multi-agent",
          "  days measure attention time, not keyboard time.",
          "- Subagent events overlap their parent session; pooling merges overlaps, so",
          "  concurrent agents don't double-count. Never sum per-file hours.",
          "- Week totals are computed from unrounded durations; rounded day figures may not",
          "  sum to them exactly."]
    (out / "time-report.md").write_text("\n".join(L) + "\n")

    # ---------------- timesheet (local tz) ----------------
    tz = ZoneInfo(args.tz) if args.tz != "UTC" else timezone.utc
    dbw = split_by_day(b_win, tz)
    e_act, e_win = daily_totals(split_by_day(b_act, tz)), daily_totals(dbw)
    wl = weekly(e_act)
    T = [
        "# Timesheet",
        "",
        f"All times **{args.tz}**. One row per work block: a stretch of activity with no",
        f"silence longer than {args.gap_window:g} minutes between events. Displayed block starts include",
        f"a {args.lead_window:g}-minute lead-in, so visible gaps read {args.lead_window:g} minutes shorter than the",
        "underlying silence. Blocks crossing local midnight split at the boundary — the prior",
        "row ends `00:00` and the block continues on the next row; a block that *ends* exactly",
        "at midnight is credited wholly to the prior day. **Active** excludes gaps over",
        f"{args.gap_active:g} minutes, so it is ≤ the block-window sum. Dates with no events are omitted",
        "(zero-activity days). Week and grand totals use unrounded durations and may not",
        "equal the sum of rounded day figures.",
        "",
    ]
    cur = None
    for d in sorted(dbw):
        wk = d.strftime("%G-W%V")
        if wk != cur:
            cur = wk
            partial = " (partial — dataset ends here)" if d.strftime("%G-W%V") == max(dbw).strftime("%G-W%V") else ""
            T += [f"## Week {wk}{partial} — {H(wl.get(wk, timedelta())):.1f} h active", ""]
        parts = [f"`{s:%H:%M}–{e:%H:%M}`" for s, e in sorted(dbw[d])]
        T.append(f"**{d} ({d:%a})** — window {H(e_win[d]):.1f} h, active {H(e_act.get(d, timedelta())):.1f} h  ")
        for i in range(0, len(parts), 7):
            T.append(", ".join(parts[i:i + 7]) + ("  " if i + 7 < len(parts) else ""))
        T.append("")
    g_act, g_win = sum(e_act.values(), timedelta()), sum(e_win.values(), timedelta())
    T += ["---", f"**Totals: {H(g_act):.1f} h active / {H(g_win):.1f} h in work windows, across "
          f"{len(dbw)} active days ({min(dbw)} → {max(dbw)} local dates).**"]
    (out / "timesheet.md").write_text("\n".join(T) + "\n")
    print(f"wrote {out}/time-report.md and {out}/timesheet.md — "
          f"{H(t_act):.1f} h active / {H(t_win):.1f} h windows over {len(d_win)} UTC days")


if __name__ == "__main__":
    main()
