#!/usr/bin/env python3
"""
Kickoff Lab — data fetcher.

Pulls everything build_data.py needs into data/raw/, always for whatever
season is current on the day this runs (no hardcoded year anywhere), and
always for every gameweek that has actually finished — not just GW1. That
second part matters: an earlier version of this pipeline hardcoded "GW1" and
would have quietly kept using GW1-only stats forever, no matter how many
gameweeks were later played. Re-running this script is what "always
up-to-date" cashes out to.

Sources (both community-maintained mirrors of the official FPL data):
  - olbauday/FPL-Elo-Insights   -> official FPL fields + Opta-style match
                                    stats + ClubElo team ratings, refreshed
                                    twice daily (07:30 / 17:30 UTC)
  - vaastav/Fantasy-Premier-League -> the full 380-fixture schedule with
                                    official FPL fixture-difficulty ratings

Usage:
    python scripts/fetch_data.py [--out data/raw] [--season-override 2026-2027]
"""
from __future__ import annotations
import argparse, concurrent.futures as cf, datetime as dt, io, sys, time
import urllib.request, urllib.error, urllib.parse

ELO_BASE = "https://raw.githubusercontent.com/olbauday/FPL-Elo-Insights/main/data"
VAASTAV_BASE = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"
UA = {"User-Agent": "kickoff-lab-fetcher/1.0 (+https://github.com/)"}


def log(msg: str) -> None:
    print(msg, flush=True)


def get(url: str, tries: int = 4, timeout: int = 30) -> bytes | None:
    """GET url, returning bytes on 200 and None on 404. Retries other failures
    (timeouts, 5xx, transient DNS/proxy hiccups) with backoff; raises after
    the last attempt so a real outage fails the workflow loudly rather than
    silently shipping stale or partial data."""
    last_err = None
    safe_url = urllib.parse.quote(url, safe=":/%")
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(safe_url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last_err = e
        except Exception as e:  # noqa: BLE001 - want to retry any transient failure
            last_err = e
        if attempt < tries:
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_err}")


def fetch_many(jobs: list[tuple[str, "os.PathLike | str"]], workers: int = 12) -> list[str]:
    """Download (url, dest_path) pairs in parallel. Returns dest paths that
    came back 404 (so callers can tell 'not played yet' from 'network broke')."""
    import os
    missing = []

    def work(job):
        url, dest = job
        data = get(url)
        if data is None:
            return dest, None
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(data)
        return dest, data

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for dest, data in ex.map(work, jobs):
            if data is None:
                missing.append(dest)
    return missing


def current_season(today: dt.date) -> tuple[str, str]:
    """Return (this season, last season) as 'YYYY-YYYY+1' strings.
    The Premier League season flips over around July; anything from July
    onward counts as the start of a new season."""
    start_year = today.year if today.month >= 7 else today.year - 1
    this_season = f"{start_year}-{start_year + 1}"
    last_season = f"{start_year - 1}-{start_year}"
    return this_season, last_season


def short(season: str) -> str:
    """'2026-2027' -> '2026-27', the format vaastav's repo uses."""
    a, b = season.split("-")
    return f"{a}-{b[2:]}"


def fetch_season_master(season: str, out: str) -> None:
    """players.csv / teams.csv / playerstats.csv / gameweek_summaries.csv —
    the season-level files that live at the root of data/{season}/."""
    base = f"{ELO_BASE}/{season}"
    jobs = [(f"{base}/{f}.csv", f"{out}/{f}.csv")
            for f in ("players", "teams", "playerstats", "gameweek_summaries")]
    missing = fetch_many(jobs)
    if missing:
        raise RuntimeError(f"season master files missing for {season}: {missing} "
                            f"— has FPL-Elo-Insights published this season yet?")


def finished_gameweeks(gw_summary_path: str) -> list[int]:
    import csv
    ids = []
    with open(gw_summary_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("finished", "").strip().lower() == "true":
                ids.append(int(row["id"]))
    return sorted(ids)


def fetch_gw_match_stats(season: str, gws: list[int], out: str, label: str) -> None:
    """matches.csv + playermatchstats.csv for each gameweek, filtered to the
    Premier League tournament (so cup/European games don't leak into the
    team-strength or player-rate numbers)."""
    base = f"{ELO_BASE}/{season}/By Tournament/Premier League"
    jobs = []
    for gw in gws:
        jobs.append((f"{base}/GW{gw}/matches.csv", f"{out}/gw{gw}_matches.csv"))
        jobs.append((f"{base}/GW{gw}/playermatchstats.csv", f"{out}/gw{gw}_pms.csv"))
    missing = fetch_many(jobs)
    if missing:
        # A missing GW inside an otherwise-finished range means the mirror
        # hasn't caught up yet — retry once after a short pause before giving up.
        log(f"  {label}: {len(missing)} file(s) missing on first pass, retrying once…")
        time.sleep(5)
        retry_jobs = []
        for path in missing:
            gw = int(path.split("gw")[1].split("_")[0])
            kind = "matches" if path.endswith("_matches.csv") else "playermatchstats"
            retry_jobs.append((f"{base}/GW{gw}/{kind}.csv", path))
        still_missing = fetch_many(retry_jobs)
        if still_missing:
            raise RuntimeError(f"{label}: still missing after retry: {still_missing}")


def fetch_official_fixtures(season_short: str, out: str) -> None:
    base = f"{VAASTAV_BASE}/{season_short}"
    jobs = [(f"{base}/fixtures.csv", f"{out}/fixtures.csv"),
            (f"{base}/teams.csv", f"{out}/teams_vaastav.csv")]
    missing = fetch_many(jobs)
    if missing:
        raise RuntimeError(f"official fixture list missing for {season_short}: {missing}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/raw")
    ap.add_argument("--season-override", default=None,
                     help="force the season, e.g. 2026-2027 (mainly for testing)")
    args = ap.parse_args()

    if args.season_override:
        this_season = args.season_override
        start_year = int(this_season.split("-")[0])
        last_season = f"{start_year - 1}-{start_year}"
    else:
        this_season, last_season = current_season(dt.date.today())

    log(f"Season: {this_season}  (previous: {last_season})")

    cur_dir = f"{args.out}/cur"
    prev_dir = f"{args.out}/prev"

    log(f"[1/5] Fetching current-season master files ({this_season})…")
    fetch_season_master(this_season, cur_dir)

    gws = finished_gameweeks(f"{cur_dir}/gameweek_summaries.csv")
    log(f"[2/5] {len(gws)} finished gameweek(s) this season: {gws or '(none yet)'}")
    if gws:
        fetch_gw_match_stats(this_season, gws, f"{cur_dir}/gw", "current season")

    log(f"[3/5] Fetching previous-season master files ({last_season})…")
    fetch_season_master(last_season, prev_dir)

    prev_gws = finished_gameweeks(f"{prev_dir}/gameweek_summaries.csv")
    log(f"[4/5] Fetching previous season's {len(prev_gws)} gameweek(s) of match stats…")
    if prev_gws:
        fetch_gw_match_stats(last_season, prev_gws, f"{prev_dir}/gw", "previous season")

    log(f"[5/5] Fetching official fixture list + FDR ({short(this_season)})…")
    fetch_official_fixtures(short(this_season), args.out)

    with open(f"{args.out}/season.txt", "w") as fh:
        fh.write(f"{this_season}\n{last_season}\n{max(gws, default=0)}\n")

    log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
