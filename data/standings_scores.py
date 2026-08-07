"""
data/standings_scores.py
=========================
Runner-proof team records, built from The Odds API scores feed instead of
ESPN (which IP-blocks GitHub Actions). Works for every sport the API covers.

How it stays accurate and gets better over time:
  - Each run fetches the last few days of FINAL scores (The Odds API caps
    daysFrom at 3) and upserts each game into a `game_scores` table on the
    shared SQLite DB.
  - The workflow commits that DB back to the repo every run, so results
    ACCUMULATE -- after a couple of weeks the table holds the whole season,
    and records approach full-season accuracy on their own.
  - get_records() aggregates all stored games for the season into the SAME
    dict shape the talent-gap / motivation factors already consume:
        {abbr: {wins, losses, runs_scored, runs_allowed, games_back, streak}}
    (runs_scored/allowed hold points-for/against so the shared Pythagorean
     math applies unchanged.)

Never raises -- returns {} on any failure so the daily run still produces a report.
"""

import logging
import sqlite3
from datetime import datetime

import requests

import config
from data.teams import normalize_team as normalize_mlb_team
from data.teams_wnba import normalize_wnba_team
from data.teams_nfl import normalize_nfl_team
from data.teams_college import normalize_college_team
from data.teams_nhl import normalize_nhl_team
from data.teams_nba import normalize_nba_team

logger = logging.getLogger(__name__)

SPORT_KEYS = {
    "WNBA": "basketball_wnba",
    "NFL": "americanfootball_nfl",
    "NCAAF": "americanfootball_ncaaf",
    "NCAAB": "basketball_ncaab",
    "NHL": "icehockey_nhl",
    "NBA": "basketball_nba",
    "MLB": "baseball_mlb",
}


def _normalizer(sport):
    if sport == "WNBA":
        return normalize_wnba_team
    if sport == "NFL":
        return normalize_nfl_team
    if sport in ("NCAAF", "NCAAB"):
        return normalize_college_team
    if sport == "NHL":
        return normalize_nhl_team
    if sport == "NBA":
        return normalize_nba_team
    return normalize_mlb_team


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
    return c


def _fetch_and_store(sport):
    key = SPORT_KEYS.get(sport)
    if not key or not config.ODDS_API_KEY:
        return
    url = f"{config.ODDS_API_BASE_URL}/sports/{key}/scores"
    try:
        resp = requests.get(url, params={"apiKey": config.ODDS_API_KEY, "daysFrom": 3}, timeout=15)
        resp.raise_for_status()
        events = resp.json()
    except Exception as exc:
        logger.warning("%s scores fetch failed: %s", sport, exc)
        return

    normalize = _normalizer(sport)
    rows = []
    for ev in events if isinstance(events, list) else []:
        if not ev.get("completed"):
            continue
        scores = ev.get("scores") or []
        if len(scores) < 2:
            continue
        by_name = {}
        for s in scores:
            try:
                by_name[s.get("name")] = float(s.get("score"))
            except (TypeError, ValueError):
                pass
        home_raw = ev.get("home_team")
        away_raw = ev.get("away_team")
        if home_raw not in by_name or away_raw not in by_name:
            continue
        rows.append((
            ev.get("id"), sport, (ev.get("commence_time") or "")[:10],
            normalize(home_raw), normalize(away_raw),
            by_name[home_raw], by_name[away_raw],
        ))

    if not rows:
        return
    try:
        c = _conn()
        c.executemany(
            "INSERT OR REPLACE INTO game_scores (game_key, sport, date, home_team, away_team, home_pts, away_pts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        c.commit()
        c.close()
        logger.info("%s scores: stored/updated %d final game(s).", sport, len(rows))
    except Exception as exc:
        logger.warning("%s scores store failed: %s", sport, exc)


def get_records(sport, season=None):
    """Fetch+store latest results, then aggregate ALL stored games for the
    season into the standings dict. {} if nothing stored yet."""
    _fetch_and_store(sport)
    season = season or datetime.now().year
    try:
        c = _conn()
        cur = c.execute(
            "SELECT date, home_team, away_team, home_pts, away_pts FROM game_scores "
            "WHERE sport=? AND substr(date,1,4)=? ORDER BY date ASC",
            (sport, str(season)))
        games = cur.fetchall()
        c.close()
    except Exception as exc:
        logger.warning("%s records read failed: %s", sport, exc)
        return {}

    if not games:
        return {}

    agg = {}   # abbr -> dict
    order = {}  # abbr -> list of "W"/"L" in date order (for streak)
    for date, home, away, hp, ap in games:
        for team in (home, away):
            agg.setdefault(team, {"wins": 0, "losses": 0, "pf": 0.0, "pa": 0.0})
            order.setdefault(team, [])
        home_won = hp > ap
        agg[home]["pf"] += hp; agg[home]["pa"] += ap
        agg[away]["pf"] += ap; agg[away]["pa"] += hp
        agg[home]["wins" if home_won else "losses"] += 1
        agg[away]["losses" if home_won else "wins"] += 1
        order[home].append("W" if home_won else "L")
        order[away].append("L" if home_won else "W")

    best_wins = max((v["wins"] for v in agg.values()), default=0)
    best_losses = min((v["losses"] for v in agg.values()), default=0)

    records = {}
    for abbr, v in agg.items():
        seq = order[abbr]
        streak = 0
        if seq:
            last = seq[-1]
            for r in reversed(seq):
                if r != last:
                    break
                streak += 1
            streak = streak if last == "W" else -streak
        gb = ((best_wins - v["wins"]) + (v["losses"] - best_losses)) / 2.0
        records[abbr] = {
            "wins": v["wins"], "losses": v["losses"],
            "runs_scored": round(v["pf"], 1), "runs_allowed": round(v["pa"], 1),
            "games_back": max(0.0, gb), "streak": streak,
        }
    return records
