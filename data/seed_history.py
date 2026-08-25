"""
data/seed_history.py
=====================
One-time-per-season backfill of LAST season's final scores into the shared
`game_scores` table -- the same table data/standings_scores.py aggregates into
team records, and therefore the same data engine/totals.py projects totals from.

WHY THIS EXISTS: the totals model needs each team's points-scored and
points-allowed per game. Those came only from results our own runs had stored,
and the store starts empty -- so at Week 1 of a season there was NO scoring
history for any team and totals produced nothing for weeks. That was a data
gap, not a real "wait for the sample" rule: last season's results exist and are
the correct opening baseline for a projection.

HOW: ESPN's scoreboard accepts a DATE RANGE, so a whole season is one request
(verified: 2025 college football = 911 completed games across 68 dates in a
single call) rather than ~100 daily calls. Costs zero Odds API credits.

Seeding is idempotent and self-marking: a marker row in stats_cache records
that a sport/season was seeded, so this runs once and never repeats. Rows go in
with INSERT OR REPLACE keyed on the ESPN event id, so even a re-run can't
double-count a game.

NOTE ON BLENDING: seeded rows carry last season's dates, so once the new season
starts, get_records(season=<current year>) naturally reads only current-season
games -- the seed is what makes the FIRST weeks work, and current results take
over on their own. Set SEED_INTO_CURRENT_SEASON to stamp seeded games with the
current season instead, which keeps prior-year form in the averages all year.
"""

import json
import logging
import sqlite3
import time

import requests

import config
from data.teams_college import normalize_college_team
from data.teams_nfl import normalize_nfl_team
from data.teams_nba import normalize_nba_team
from data.teams_nhl import normalize_nhl_team
from data.teams_wnba import normalize_wnba_team

logger = logging.getLogger(__name__)

# league path, ESPN group filter (80 = FBS), and the prior-season date range.
SEED_PLANS = {
    "NCAAF": {"path": "football/college-football", "groups": 80,
              "range": ("0823", "1213")},
    "NFL":   {"path": "football/nfl", "groups": None,
              "range": ("0904", "0110")},
    "NCAAB": {"path": "basketball/mens-college-basketball", "groups": 50,
              "range": ("1103", "0315")},
    "NBA":   {"path": "basketball/nba", "groups": None,
              "range": ("1022", "0415")},
    "NHL":   {"path": "hockey/nhl", "groups": None,
              "range": ("1008", "0415")},
    "WNBA":  {"path": "basketball/wnba", "groups": None,
              "range": ("0515", "0920")},
}

# When True, seeded games are stamped with the CURRENT season year so prior-year
# scoring stays in the averages all season. When False (default) they keep their
# real prior-year dates and only bridge the early weeks.
SEED_INTO_CURRENT_SEASON = True

HOSTS = [
    "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard",
    "https://site.web.api.espn.com/apis/site/v2/sports/{path}/scoreboard",
]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
}


def _normalizer(sport):
    if sport in ("NCAAF", "NCAAB"):
        return normalize_college_team
    if sport == "NFL":
        return normalize_nfl_team
    if sport == "NBA":
        return normalize_nba_team
    if sport == "NHL":
        return normalize_nhl_team
    if sport == "WNBA":
        return normalize_wnba_team
    return lambda x: x


def _conn():
    c = sqlite3.connect(str(config.DB_PATH))
    c.execute("""CREATE TABLE IF NOT EXISTS game_scores (
                   game_key TEXT PRIMARY KEY,
                   sport TEXT NOT NULL,
                   date TEXT NOT NULL,
                   home_team TEXT NOT NULL,
                   away_team TEXT NOT NULL,
                   home_pts REAL NOT NULL,
                   away_pts REAL NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS stats_cache (
                   key TEXT PRIMARY KEY, payload TEXT NOT NULL, cached_at REAL NOT NULL)""")
    return c


def _already_seeded(conn, marker):
    row = conn.execute("SELECT payload FROM stats_cache WHERE key=?", (marker,)).fetchone()
    return row is not None


def _mark_seeded(conn, marker, count):
    conn.execute("INSERT OR REPLACE INTO stats_cache (key, payload, cached_at) VALUES (?, ?, ?)",
                 (marker, json.dumps({"games": count}), time.time()))
    conn.commit()


def _fetch_range(path, groups, start, end):
    params = {"dates": f"{start}-{end}", "limit": 1000}
    if groups:
        params["groups"] = groups
    for tpl in HOSTS:
        try:
            resp = requests.get(tpl.format(path=path), params=params, headers=HEADERS, timeout=45)
            resp.raise_for_status()
            events = resp.json().get("events", [])
            if events:
                return events
        except Exception as exc:
            logger.debug("Seed fetch failed (%s): %s", tpl, exc)
    return []


def ensure_season_seeded(sport, current_season):
    """Backfill last season's finals for `sport` if we haven't already.
    Safe to call on every run -- returns immediately once the marker exists."""
    plan = SEED_PLANS.get(sport)
    if not plan:
        return 0

    prior = current_season - 1
    marker = f"seed:{sport}:{prior}"
    try:
        conn = _conn()
    except Exception as exc:
        logger.warning("Seed %s: cannot open DB: %s", sport, exc)
        return 0

    if _already_seeded(conn, marker):
        conn.close()
        return 0

    start_md, end_md = plan["range"]
    # A range ending earlier in the calendar than it starts wraps the new year.
    start = f"{prior}{start_md}"
    end = f"{prior if end_md >= start_md else prior + 1}{end_md}"

    logger.info("Seeding %s history from %s-%s (one-time, prior season).", sport, start, end)
    events = _fetch_range(plan["path"], plan.get("groups"), start, end)
    if not events:
        logger.warning("Seed %s: ESPN returned no events for %s-%s -- will retry next run.",
                       sport, start, end)
        conn.close()
        return 0

    normalize = _normalizer(sport)
    rows = []
    for ev in events:
        for comp in ev.get("competitions", []):
            if not (comp.get("status", {}).get("type", {}) or {}).get("completed"):
                continue
            home = away = None
            for c in comp.get("competitors", []):
                team = c.get("team") or {}
                name = (team.get("displayName") or team.get("location")
                        or team.get("abbreviation") or "")
                try:
                    score = float(c.get("score"))
                except (TypeError, ValueError):
                    score = None
                if score is None or not name:
                    continue
                if c.get("homeAway") == "home":
                    home = (normalize(name), score)
                elif c.get("homeAway") == "away":
                    away = (normalize(name), score)
            if not home or not away:
                continue
            real_date = (ev.get("date") or "")[:10]
            stamp_date = (f"{current_season}{real_date[4:]}"
                          if SEED_INTO_CURRENT_SEASON and len(real_date) == 10 else real_date)
            rows.append((f"seed-{sport}-{ev.get('id')}", sport, stamp_date,
                         home[0], away[0], home[1], away[1]))

    if not rows:
        logger.warning("Seed %s: no completed games parsed out of %d events.", sport, len(events))
        conn.close()
        return 0

    try:
        conn.executemany(
            "INSERT OR REPLACE INTO game_scores "
            "(game_key, sport, date, home_team, away_team, home_pts, away_pts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
        _mark_seeded(conn, marker, len(rows))
        logger.info("Seeded %s: %d prior-season games stored -- totals and records now have "
                    "a real baseline from day one.", sport, len(rows))
    except Exception as exc:
        logger.warning("Seed %s store failed: %s", sport, exc)
        conn.close()
        return 0

    conn.close()
    return len(rows)
