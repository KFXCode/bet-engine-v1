"""
data/td_settle.py
==================
Who actually scored a touchdown, for grading anytime-TD props.

Source: ESPN's game summary endpoint
    /sports/football/nfl/summary?event=<espn_event_id>
whose boxscore carries per-player rushing and receiving TD columns. Reading
the TD column is far more reliable than parsing scoring-play sentences like
"Joshua Dobbs 50 Yd Rush", which vary in wording and would break on any
unusual play description.

THE ID PROBLEM: our NFL game_ids are Odds API hashes ("nfl-<hash>"), not ESPN
event ids, so we can't call summary directly. We first locate the ESPN event
by DATE + TEAM ABBREVIATIONS off the scoreboard (through data/espn_fetch.py,
so a single blocked ESPN host can't kill grading -- that exact failure is what
left every non-MLB pick stuck on "pending" for weeks), then pull its summary.

Returns None (never an empty set) whenever the game isn't final or anything
fails, so the grader leaves the pick pending instead of wrongly scoring it a
loss. An empty set is a real answer: the game finished and nobody we care
about scored.
"""

import logging

import requests

from data.espn_fetch import fetch_scoreboard_events

logger = logging.getLogger(__name__)

SUMMARY_HOSTS = [
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary",
    "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/summary",
]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
}

# Same sport-aware alias problem as final_scores.py: ESPN says WSH, we say WAS.
NFL_ALIASES = {
    "WSH": "WAS", "LA": "LAR", "GNB": "GB", "JAC": "JAX", "KAN": "KC",
    "LVR": "LV", "OAK": "LV", "NWE": "NE", "NOR": "NO", "SFO": "SF",
    "TAM": "TB", "ARZ": "ARI", "BLT": "BAL", "CLV": "CLE", "HST": "HOU",
}

TD_STAT_GROUPS = ("rushing", "receiving")


def _canon(abbr):
    a = (abbr or "").strip().upper()
    return NFL_ALIASES.get(a, a)


def _find_event_id(date_str, home_team, away_team):
    events = fetch_scoreboard_events("football/nfl", date_str, season_types=(None, 1, 2, 3))
    if not events:
        return None
    want_home, want_away = _canon(home_team), _canon(away_team)
    single_side = []
    for ev in events:
        for comp in ev.get("competitions", []):
            if not (comp.get("status", {}).get("type", {}) or {}).get("completed"):
                continue
            abbrs = {}
            for c in comp.get("competitors", []):
                abbrs[c.get("homeAway")] = _canon((c.get("team") or {}).get("abbreviation"))
            h, a = abbrs.get("home"), abbrs.get("away")
            if {h, a} == {want_home, want_away}:
                return ev.get("id")
            if want_home in (h, a) or want_away in (h, a):
                single_side.append(ev.get("id"))
    if len(single_side) == 1:
        logger.info("NFL TD settle %s: matched %s @ %s on one side only -- using event %s.",
                    date_str, want_away, want_home, single_side[0])
        return single_side[0]
    return None


def _fetch_summary(event_id):
    for base in SUMMARY_HOSTS:
        try:
            resp = requests.get(base, params={"event": event_id}, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.debug("NFL summary fetch failed (%s / %s): %s", base, event_id, exc)
    return None


def get_td_scorers(date_str, home_team, away_team):
    """Set of player names who scored >=1 rushing or receiving TD in this
    finished game, or None if it isn't final / can't be read."""
    event_id = _find_event_id(date_str, home_team, away_team)
    if not event_id:
        logger.debug("NFL TD settle: no completed ESPN event for %s @ %s on %s.",
                     away_team, home_team, date_str)
        return None

    payload = _fetch_summary(event_id)
    if not payload:
        return None

    teams = ((payload.get("boxscore") or {}).get("players") or [])
    if not teams:
        logger.debug("NFL TD settle: summary for %s has no player boxscore yet.", event_id)
        return None

    scorers = set()
    saw_any_stats = False
    for team_block in teams:
        for group in team_block.get("statistics", []) or []:
            if group.get("name") not in TD_STAT_GROUPS:
                continue
            labels = [str(l).upper() for l in (group.get("labels") or [])]
            if "TD" not in labels:
                continue
            td_idx = labels.index("TD")
            for athlete in group.get("athletes", []) or []:
                stats = athlete.get("stats") or []
                if td_idx >= len(stats):
                    continue
                saw_any_stats = True
                try:
                    tds = int(str(stats[td_idx]).strip() or 0)
                except (TypeError, ValueError):
                    continue
                if tds > 0:
                    name = ((athlete.get("athlete") or {}).get("displayName")
                            or (athlete.get("athlete") or {}).get("shortName"))
                    if name:
                        scorers.add(name)

    if not saw_any_stats:
        return None
    logger.info("NFL TD settle %s (%s @ %s): %d scorer(s) -- %s",
                date_str, away_team, home_team, len(scorers),
                ", ".join(sorted(scorers)) or "(none)")
    return scorers
