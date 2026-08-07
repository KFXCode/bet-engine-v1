"""
data/standings_espn.py
=======================
Generic ESPN standings feed for NBA, NHL, NFL, NCAAF, NCAAB -- same free,
no-key endpoint family the WNBA feed uses, returning the SAME dict shape as
data/standings.py so the shared talent-gap (Pythagorean win% from points/
goals for-against) and motivation (games back + streak) factors work for
every sport with zero factor changes:

    {abbr: {wins, losses, runs_scored, runs_allowed, games_back, streak}}

ESPN blocks default python-requests from datacenter IPs, so we send a browser
User-Agent (BROWSER_HEADERS) -- the same fix the schedule providers use.
Never raises -- returns {} on failure so the daily run still produces a report.
Off-season sports return no games, so this is never called for them until
their season opens.
"""

import logging

import requests

from data.teams_nba import normalize_nba_team
from data.teams_nhl import normalize_nhl_team
from data.teams_nfl import normalize_nfl_team
from data.teams_college import normalize_college_team

logger = logging.getLogger(__name__)

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.espn.com/",
}

# sport -> (ESPN league path, normalize fn, (for-stat names), (against-stat names))
_SPORT_CONFIG = {
    "NBA":   ("basketball/nba", normalize_nba_team,
              ("pointsfor", "avgpointsfor", "points"), ("pointsagainst", "avgpointsagainst")),
    "NHL":   ("hockey/nhl", normalize_nhl_team,
              ("pointsfor", "goalsfor", "goals"), ("pointsagainst", "goalsagainst")),
    "NFL":   ("football/nfl", normalize_nfl_team,
              ("pointsfor", "avgpointsfor", "points"), ("pointsagainst", "avgpointsagainst")),
    "NCAAF": ("football/college-football", normalize_college_team,
              ("pointsfor", "avgpointsfor", "points"), ("pointsagainst", "avgpointsagainst")),
    "NCAAB": ("basketball/mens-college-basketball", normalize_college_team,
              ("pointsfor", "avgpointsfor", "points"), ("pointsagainst", "avgpointsagainst")),
}

_STANDINGS_URL = "https://site.api.espn.com/apis/v2/sports/{path}/standings"


def get_all_records_for_sport(sport, season=None):
    cfg = _SPORT_CONFIG.get(sport)
    if not cfg:
        return {}
    path, normalize, for_names, against_names = cfg
    try:
        params = {"season": season} if season else {}
        resp = requests.get(_STANDINGS_URL.format(path=path), params=params,
                            headers=BROWSER_HEADERS, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning("%s standings fetch failed: %s", sport, exc)
        return {}

    records = {}
    entries = _collect_entries(payload)
    logger.info("%s standings: %d team entries.", sport, len(entries))
    for entry in entries:
        try:
            team = entry.get("team", {})
            name = team.get("displayName") or team.get("name") or ""
            abbr = normalize(name)
            if not abbr:
                continue
            stats = entry.get("stats", []) or []
            records[abbr] = {
                "wins": _to_int(_stat(stats, "wins")),
                "losses": _to_int(_stat(stats, "losses")),
                "runs_scored": _stat_num(stats, for_names),
                "runs_allowed": _stat_num(stats, against_names),
                "games_back": _stat_num(stats, ("gamesbehind", "gamesback")) or 0.0,
                "streak": _to_int(_stat(stats, "streak")) or 0,
            }
        except Exception as exc:
            logger.debug("Skipping a %s standings entry: %s", sport, exc)
    return records


def _collect_entries(payload):
    entries = []
    std = payload.get("standings")
    if isinstance(std, dict) and std.get("entries"):
        entries.extend(std["entries"])
    for child in payload.get("children", []) or []:
        cstd = child.get("standings", {})
        if isinstance(cstd, dict) and cstd.get("entries"):
            entries.extend(cstd["entries"])
    return entries


def _stat(stats, *names):
    for s in stats:
        key = (s.get("name") or s.get("type") or s.get("abbreviation") or "").lower()
        if key in names:
            val = s.get("value")
            if val is None:
                val = s.get("displayValue")
            return val
    return None


def _stat_num(stats, names):
    return _to_num(_stat(stats, *names))


def _to_num(v):
    if v in (None, "", "-"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v):
    n = _to_num(v)
    return int(n) if n is not None else None
