"""
data/seed_history.py
=====================
One-time-per-season backfill of LAST season's final scores into the shared
`game_scores` table -- the same table data/standings_scores.py aggregates into
team records, and therefore the same data engine/totals.py projects totals from.

WHY THIS EXISTS: the totals model needs each team's points scored and allowed
per game. Those came only from results our own runs had stored, and the store
starts empty -- so at Week 1 no team had scoring history and totals produced
nothing for weeks. That was a data gap, not a real sampling rule: last
season's results exist and are the correct opening baseline.

WHY MONTHLY CHUNKS (v2): the first version pulled a whole season in ONE
request. ESPN caps a scoreboard response at 1000 events, so the big leagues
came back truncated -- NBA stopped at 996 and NHL at 999, meaning partial
seasons masquerading as complete ones -- and college basketball, the largest
slate of all, timed out and seeded nothing at all. Fetching month by month
keeps every response small enough to return in full and to finish inside the
timeout. It costs ~30 requests once per season and zero Odds API credits.

Seeding is idempotent and self-marking (marker rows in stats_cache), and rows
are keyed on the ESPN event id with INSERT OR REPLACE, so re-runs can never
double-count a game. The marker key carries a VERSION -- bumping it is how a
fix like this re-seeds sports that were already marked done under the old,
truncated logic.
"""

import json
import logging
import sqlite3
import time
from datetime import date, timedelta

import requests

import config
from data.teams_college import normalize_college_team
from data.teams_nfl import normalize_nfl_team
from data.teams_nba import normalize_nba_team
from data.teams_nhl import normalize_nhl_team
from data.teams_wnba import normalize_wnba_team

logger = logging.getLogger(__name__)

# Bump this when the seeding logic changes in a way that needs a re-pull.
SEED_VERSION = 2

# league path, ESPN group filter, and the prior-season (month, day) range.
SEED_PLANS = {
    "NCAAF": {"path": "football/college-football", "groups": 80,
              "start": (8, 23), "end": (12, 13)},
    "NFL":   {"path": "football/nfl", "groups": None,
              "start": (9, 4), "end": (1, 10)},
    "NCAAB": {"path": "basketball/mens-college-basketball", "groups": 50,
              "start": (11, 3), "end": (3, 15)},
    "NBA":   {"path": "basketball/nba", "groups": None,
              "start": (10, 22), "end": (4, 15)},
    "NHL":   {"path": "hockey/nhl", "groups": None,
              "start": (10, 8), "end": (4, 15)},
    "WNBA":  {"path": "basketball/wnba", "groups": None,
              "start": (5, 15), "end": (9, 20)},
}

# Seeded games are stamped with the CURRENT season year so prior-year scoring
# stays in the averages all season. False keeps their real prior-year dates,
# which means the seed only bridges the opening weeks.
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


def _month_windows(start_date, end_date):
    """[(YYYYMMDD, YYYYMMDD)] calendar-month slices covering the range."""
    windows = []
    cursor = start_date
    while cursor <= end_date:
        if cursor.month == 12:
            month_end = date(cursor.year, 12, 31)
        else:
            month_end = date(cursor.year, cursor.month + 1, 1) - timedelta(days=1)
        window_end = min(month_end, end_date)
        windows.append((cursor.strftime("%Y%m%d"), window_end.strftime("%Y%m%d")))
        cursor = window_end + timedelta(days=1)
    return windows


def _fetch_window(path, groups, start, end):
    params = {"dates": f"{start}-{end}", "limit": 1000}
    if groups:
        params["groups"] = groups
    for tpl in HOSTS:
        try:
            resp = requests.get(tpl.format(path=path), params=params, headers=HEADERS, timeout=40)
            resp.raise_for_status()
            events = resp.json().get("events", [])
            if events:
                if len(events) >= 1000:
                    logger.warning("Seed window %s-%s hit the 1000-event cap -- may be truncated.",
                                   start, end)
                return events
        except Exception as exc:
            logger.debug("Seed fetch failed (%s %s-%s): %s", tpl, start, end, exc)
    return []


def _parse_rows(sport, events, current_season):
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
    return rows


def ensure_season_seeded(sport, current_season):
    """Backfill last season's finals for `sport` if not already seeded under
    the current SEED_VERSION. Safe to call every run."""
    plan = SEED_PLANS.get(sport)
    if not plan:
        return 0

    prior = current_season - 1
    marker = f"seed{SEED_VERSION}:{sport}:{prior}"
    try:
        conn = _conn()
    except Exception as exc:
        logger.warning("Seed %s: cannot open DB: %s", sport, exc)
        return 0

    if conn.execute("SELECT 1 FROM stats_cache WHERE key=?", (marker,)).fetchone():
        conn.close()
        return 0

    s_month, s_day = plan["start"]
    e_month, e_day = plan["end"]
    start_date = date(prior, s_month, s_day)
    # An end month earlier than the start month means the season crosses years.
    end_year = prior if (e_month, e_day) >= (s_month, s_day) else prior + 1
    end_date = date(end_year, e_month, e_day)

    windows = _month_windows(start_date, end_date)
    logger.info("Seeding %s prior season (%s -> %s) in %d monthly window(s).",
                sport, start_date, end_date, len(windows))

    all_rows = []
    seen_keys = set()
    for start, end in windows:
        events = _fetch_window(plan["path"], plan.get("groups"), start, end)
        if not events:
            continue
        for row in _parse_rows(sport, events, current_season):
            if row[0] in seen_keys:
                continue
            seen_keys.add(row[0])
            all_rows.append(row)

    if not all_rows:
        logger.warning("Seed %s: no completed games parsed -- will retry next run.", sport)
        conn.close()
        return 0

    try:
        conn.executemany(
            "INSERT OR REPLACE INTO game_scores "
            "(game_key, sport, date, home_team, away_team, home_pts, away_pts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", all_rows)
        conn.execute("INSERT OR REPLACE INTO stats_cache (key, payload, cached_at) VALUES (?, ?, ?)",
                     (marker, json.dumps({"games": len(all_rows)}), time.time()))
        conn.commit()
        logger.info("Seeded %s: %d prior-season games stored across %d window(s).",
                    sport, len(all_rows), len(windows))
    except Exception as exc:
        logger.warning("Seed %s store failed: %s", sport, exc)
        conn.close()
        return 0

    conn.close()
    return len(all_rows)
