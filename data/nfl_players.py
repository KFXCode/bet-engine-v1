"""
data/nfl_players.py
====================
Skill-position rosters and SEASON STAT PROFILES for NFL player props, from
free ESPN endpoints (verified working from the runner):

    roster -> /sports/football/nfl/teams/{espn_id}/roster
    stats  -> /common/v3/sports/football/nfl/athletes/{id}/stats

Originally this pulled touchdowns only. It now returns a FULL per-game profile
-- passing / rushing / receiving yards, attempts, completions, receptions,
targets -- because the prop board covers passing yards, rushing yards,
receiving yards, receptions and QB pass TDs, and every one of those models
needs a per-game rate plus the volume behind it.

WHY PER-GAME RATES AND NOT TOTALS: a prop is a single-game question. A back
with 900 rush yards means nothing until you know whether that came in 6 games
or 16. Every field below is stored as both the season total and the per-game
average, and models should read the per-game number.

TWO ESPN QUIRKS THIS HANDLES:
  1. Stat rows come back for MANY seasons and NOT in a guaranteed order (spot
     checks returned 2017 first for one player, 2021 for another). Never trust
     statistics[0] -- we scan every row and keep the newest season.
  2. Early in a season the current year has no data at all, so we fall back to
     the most recent completed season. Without that, every Week 1 prop would
     be modelled off zeros.

Everything is cached 24h in the shared stats_cache table, and every failure is
swallowed (returns None/empty) so the daily run always produces a report.
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


def get_player_profile(player_id, name=None):
    """Full season profile for a skill player, with per-game rates.

    Returns None when the player has no usable stat history at all.
    {
      season, games,
      pass_yds, pass_att, pass_cmp, pass_td,   + *_pg per-game versions
      rush_yds, rush_att, rush_td,             + *_pg
      rec_yds, rec, targets, rec_td,           + *_pg
      total_td, td_per_game, touches
    }
    """
    if not player_id:
        return None
    key = f"nfl_profile:{player_id}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    payload = _get_json(STATS_HOSTS, pid=player_id)
    if not payload:
        return None

    cats = {c.get("name"): c for c in (payload.get("categories") or [])}
    prof = {
        "season": None, "games": 0,
        "pass_yds": 0.0, "pass_att": 0.0, "pass_cmp": 0.0, "pass_td": 0.0,
        "rush_yds": 0.0, "rush_att": 0.0, "rush_td": 0.0,
        "rec_yds": 0.0, "rec": 0.0, "targets": 0.0, "rec_td": 0.0,
    }
    season_used = None
    games = 0.0

    # PASSING
    cat = cats.get("passing")
    if cat:
        row, year = _newest_season_row(cat)
        if row:
            v = _labelled(cat, row)
            prof["pass_yds"] = _num(v.get("YDS"))
            prof["pass_att"] = _num(v.get("ATT"))
            prof["pass_cmp"] = _num(v.get("CMP"))
            prof["pass_td"] = _num(v.get("TD"))
            games = max(games, _num(v.get("GP")))
            season_used = year if season_used is None or (year and year > season_used) else season_used

    # RUSHING
    cat = cats.get("rushing")
    if cat:
        row, year = _newest_season_row(cat)
        if row:
            v = _labelled(cat, row)
            prof["rush_yds"] = _num(v.get("YDS"))
            prof["rush_att"] = _num(v.get("CAR")) or _num(v.get("ATT"))
            prof["rush_td"] = _num(v.get("TD"))
            games = max(games, _num(v.get("GP")))
            season_used = year if season_used is None or (year and year > season_used) else season_used

    # RECEIVING
    cat = cats.get("receiving")
    if cat:
        row, year = _newest_season_row(cat)
        if row:
            v = _labelled(cat, row)
            prof["rec_yds"] = _num(v.get("YDS"))
            prof["rec"] = _num(v.get("REC"))
            prof["targets"] = _num(v.get("TGTS")) or _num(v.get("TGT"))
            prof["rec_td"] = _num(v.get("TD"))
            games = max(games, _num(v.get("GP")))
            season_used = year if season_used is None or (year and year > season_used) else season_used

    total_td = prof["rush_td"] + prof["rec_td"]
    if games <= 0 and total_td <= 0 and prof["pass_yds"] <= 0:
        _cache_set(key, None)
        return None

    prof["season"] = season_used
    prof["games"] = int(games)
    prof["total_td"] = int(total_td)
    prof["touches"] = int(prof["rush_att"] + prof["rec"])

    # Per-game rates -- what every prop model actually reads.
    g = games if games > 0 else 1.0
    for base in ("pass_yds", "pass_att", "pass_cmp", "pass_td",
                 "rush_yds", "rush_att", "rush_td",
                 "rec_yds", "rec", "targets", "rec_td"):
        prof[f"{base}_pg"] = round(prof[base] / g, 3)
    prof["td_per_game"] = round(total_td / g, 3)

    _cache_set(key, prof)
    return prof


def get_td_profile(player_id, name=None):
    """Back-compat shim for engine/td_props.py, which only needs the TD view.
    Kept so the TD model didn't have to change when the profile got wider."""
    prof = get_player_profile(player_id, name)
    if not prof:
        return None
    return {
        "season": prof["season"],
        "games": prof["games"],
        "rush_td": int(prof["rush_td"]),
        "rec_td": int(prof["rec_td"]),
        "total_td": prof["total_td"],
        "td_per_game": prof["td_per_game"],
        "touches": prof["touches"],
        "targets": int(prof["targets"]),
    }
