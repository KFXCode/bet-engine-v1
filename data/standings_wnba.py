"""
data/standings_wnba.py
=======================
WNBA team records from ESPN's public standings endpoint (free, no key).
Returns the SAME dict shape as data/standings.py (MLB) so the existing
talent-gap (Pythagorean win% from points for/against) and motivation
(games back + streak) factors work for WNBA with zero changes:

    {abbr: {wins, losses, runs_scored, runs_allowed, games_back, streak}}

"runs_scored"/"runs_allowed" hold points-for/points-against so the shared
Pythagorean math applies. Keyed by the same normalize_wnba_team() output the
schedule provider uses, so game team abbrs line up. Never raises -- returns
{} on any failure so the daily run still produces a report.
"""

import logging

import requests

from data.teams_wnba import normalize_wnba_team

logger = logging.getLogger(__name__)

ESPN_STANDINGS = "https://site.api.espn.com/apis/v2/sports/basketball/wnba/standings"


def _stat(stats, *names):
    """Pull the first matching stat value by name/type/abbreviation."""
    for s in stats:
        key = (s.get("name") or s.get("type") or s.get("abbreviation") or "").lower()
        if key in names:
            val = s.get("value")
            if val is None:
                val = s.get("displayValue")
            return val
    return None


def get_all_wnba_records(season=None):
    try:
        params = {"season": season} if season else {}
        resp = requests.get(ESPN_STANDINGS, params=params, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning("WNBA standings fetch failed: %s", exc)
        return {}

    records = {}
    entries = _collect_entries(payload)
    for entry in entries:
        try:
            team = entry.get("team", {})
            name = team.get("displayName") or team.get("name") or ""
            abbr = normalize_wnba_team(name)
            if not abbr:
                continue
            stats = entry.get("stats", []) or []
            wins = _to_num(_stat(stats, "wins"))
            losses = _to_num(_stat(stats, "losses"))
            pf = _to_num(_stat(stats, "pointsfor", "avgpointsfor", "points"))
            pa = _to_num(_stat(stats, "pointsagainst", "avgpointsagainst"))
            gb = _to_num(_stat(stats, "gamesbehind", "gamesback"))
            streak = _to_num(_stat(stats, "streak")) or 0
            records[abbr] = {
                "wins": int(wins) if wins is not None else None,
                "losses": int(losses) if losses is not None else None,
                "runs_scored": pf,
                "runs_allowed": pa,
                "games_back": gb if gb is not None else 0.0,
                "streak": int(streak),
            }
        except Exception as exc:
            logger.debug("Skipping a WNBA standings entry: %s", exc)
    return records


def _collect_entries(payload):
    """ESPN nests standings.entries under children (conferences) or at the
    top level depending on the endpoint shape -- gather from wherever."""
    entries = []
    std = payload.get("standings")
    if isinstance(std, dict) and std.get("entries"):
        entries.extend(std["entries"])
    for child in payload.get("children", []) or []:
        cstd = child.get("standings", {})
        if isinstance(cstd, dict) and cstd.get("entries"):
            entries.extend(cstd["entries"])
    return entries


def _to_num(v):
    if v in (None, "", "-"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
