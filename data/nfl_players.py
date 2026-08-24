"""
data/nfl_players.py
====================
Skill-position rosters + season TD production for NFL touchdown props, from
free ESPN endpoints (verified working from the runner):

    roster   -> /sports/football/nfl/teams/{espn_id}/roster
    stats    -> /common/v3/sports/football/nfl/athletes/{id}/stats

Only skill positions can realistically score, so we filter to RB/WR/TE/QB and
ignore the rest of the roster -- that keeps the candidate pool at ~20 players
per team instead of 90, which matters because each player costs one stats call.

IMPORTANT about seasons: ESPN returns a player's stat rows for MANY seasons and
NOT in a guaranteed order (spot checks came back with 2017 first for one player
and 2021 first for another). Never trust statistics[0] -- we scan every row and
keep the newest season. And in early season / preseason the current year has no
data at all, so we fall back to the most recent completed season as the
baseline. Without that fallback every Week 1 TD prop would score off zeros.

Everything is cached 24h in the shared stats_cache table, and every failure is
swallowed (returns empty) so the daily run always produces a report.
"""

import json
import logging
import time

import requests

import config

logger = logging.getLogger(__name__)

CACHE_TTL_HOURS = 24

# ESPN numeric team ids, keyed by OUR abbreviation (data/teams_nfl.py).
ESPN_TEAM_IDS = {
    "ATL": 1, "BUF": 2, "CHI": 3, "CIN": 4, "CLE": 5, "DAL": 6, "DEN": 7,
    "DET": 8, "GB": 9, "TEN": 10, "IND": 11, "KC": 12, "LV": 13, "LAR": 14,
    "MIA": 15, "MIN": 16, "NE": 17, "NO": 18, "NYG": 19, "NYJ": 20, "PHI": 21,
    "ARI": 22, "PIT": 23, "LAC": 24, "SF": 25, "SEA": 26, "TB": 27, "WAS": 28,
    "CAR": 29, "JAX": 30, "BAL": 33, "HOU": 34,
}

SCORING_POSITIONS = {"RB", "WR", "TE", "QB", "FB"}

ROSTER_HOSTS = [
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{tid}/roster",
    "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/teams/{tid}/roster",
]
STATS_HOSTS = [
    "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/athletes/{pid}/stats",
    "https://site.api.espn.com/apis/common/v3/sports/football/nfl/athletes/{pid}/stats",
]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
}


def _cache():
    import sqlite3
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.execute("""CREATE TABLE IF NOT EXISTS stats_cache (
        key TEXT PRIMARY KEY, payload TEXT NOT NULL, cached_at REAL NOT NULL)""")
    conn.commit()
    return conn


def _cache_get(key):
    try:
        conn = _cache()
        row = conn.execute("SELECT payload, cached_at FROM stats_cache WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        payload, cached_at = row
        if time.time() - cached_at > CACHE_TTL_HOURS * 3600:
            return None
        return json.loads(payload)
    except Exception:
        return None


def _cache_set(key, value):
    try:
        conn = _cache()
        conn.execute("INSERT OR REPLACE INTO stats_cache (key, payload, cached_at) VALUES (?, ?, ?)",
                     (key, json.dumps(value), time.time()))
        conn.commit()
    except Exception as exc:
        logger.debug("cache write failed for %s: %s", key, exc)


def _get_json(hosts, **fmt):
    for tpl in hosts:
        try:
            resp = requests.get(tpl.format(**fmt), headers=HEADERS, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.debug("fetch failed %s: %s", tpl, exc)
    return None


def get_skill_players(team_abbr):
    """[{player_id, name, position}] for the skill players on this team."""
    tid = ESPN_TEAM_IDS.get(team_abbr)
    if not tid:
        logger.debug("No ESPN team id mapped for NFL abbr %s.", team_abbr)
        return []

    key = f"nfl_roster:{team_abbr}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    payload = _get_json(ROSTER_HOSTS, tid=tid)
    if not payload:
        return []

    players = []
    for group in payload.get("athletes", []):
        for a in group.get("items", []):
            pos = ((a.get("position") or {}).get("abbreviation") or "").upper()
            if pos not in SCORING_POSITIONS:
                continue
            pid = a.get("id")
            name = a.get("displayName")
            if pid and name:
                players.append({"player_id": str(pid), "name": name, "position": pos})
    _cache_set(key, players)
    logger.info("NFL roster %s: %d skill players.", team_abbr, len(players))
    return players


def _newest_season_row(category):
    """ESPN's rows are not ordered -- pick the highest season we can parse."""
    best = None
    best_year = -1
    for row in category.get("statistics", []) or []:
        season = row.get("season") or {}
        year = season.get("year")
        try:
            year = int(year)
        except (TypeError, ValueError):
            continue
        if year > best_year:
            best_year = year
            best = row
    return best, best_year


def _labelled(category, row):
    labels = [str(l).upper() for l in (category.get("labels") or [])]
    stats = row.get("stats") or []
    out = {}
    for i, label in enumerate(labels):
        if i < len(stats):
            out[label] = stats[i]
    return out


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def get_td_profile(player_id, name=None):
    """Season TD production for a player:
        {season, games, rush_td, rec_td, total_td, td_per_game, touches, targets}
    Uses the newest season with data; in preseason / Week 1 that is last
    season, which is the right baseline rather than scoring off zeros."""
    if not player_id:
        return None
    key = f"nfl_td:{player_id}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    payload = _get_json(STATS_HOSTS, pid=player_id)
    if not payload:
        return None

    cats = {c.get("name"): c for c in (payload.get("categories") or [])}
    rush_td = rec_td = games = touches = targets = 0.0
    season_used = None

    for cat_name, td_key, touch_key in (("rushing", "TD", "CAR"), ("receiving", "TD", "REC")):
        cat = cats.get(cat_name)
        if not cat:
            continue
        row, year = _newest_season_row(cat)
        if not row:
            continue
        vals = _labelled(cat, row)
        td = _num(vals.get(td_key))
        gp = _num(vals.get("GP"))
        if cat_name == "rushing":
            rush_td = td
            touches += _num(vals.get(touch_key))
        else:
            rec_td = td
            touches += _num(vals.get(touch_key))
            targets = _num(vals.get("TGTS"))
        games = max(games, gp)
        if season_used is None or (year and year > season_used):
            season_used = year

    total_td = rush_td + rec_td
    if games <= 0 and total_td <= 0:
        _cache_set(key, None)
        return None

    profile = {
        "season": season_used,
        "games": int(games),
        "rush_td": int(rush_td),
        "rec_td": int(rec_td),
        "total_td": int(total_td),
        "td_per_game": round(total_td / games, 3) if games else 0.0,
        "touches": int(touches),
        "targets": int(targets),
    }
    _cache_set(key, profile)
    return profile
