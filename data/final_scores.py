"""
data/final_scores.py
=====================
Final scores for NON-MLB sports (WNBA, NFL, NCAAF, NCAAB, NHL, NBA) from the
ESPN scoreboard -- the same source the schedule providers use.

Why this exists: backtest/grader.py graded every pick through
statsapi.mlb.com. A WNBA/NFL/etc game_id means nothing to that endpoint, so
those picks silently stayed "pending" forever and NEVER showed up in the
History tab. This module closes that hole so every sport gets graded and
recorded, not just MLB.

Our non-MLB game_ids are hashes (e.g. "wnba-8643e7ef..."), not ESPN event ids,
so we match on DATE + TEAM ABBREVIATIONS from the stored games row instead.

get_final_score_espn(sport, date_str, home_team, away_team)
    -> (home_score, away_score) when the game is FINAL
    -> None when it isn't final yet, isn't found, or anything errors
"""

import logging

import requests

logger = logging.getLogger(__name__)

ESPN_PATHS = {
    "WNBA": "basketball/wnba",
    "NBA": "basketball/nba",
    "NCAAB": "basketball/mens-college-basketball",
    "NFL": "football/nfl",
    "NCAAF": "football/college-football",
    "NHL": "hockey/nhl",
}

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"


def _norm(abbr):
    return (abbr or "").strip().upper()


def get_final_score_espn(sport, date_str, home_team, away_team):
    path = ESPN_PATHS.get(sport)
    if not path or not date_str:
        return None
    try:
        resp = requests.get(SCOREBOARD.format(path=path),
                            params={"dates": date_str.replace("-", ""), "limit": 400},
                            timeout=20)
        resp.raise_for_status()
        events = resp.json().get("events", [])
    except Exception as exc:
        logger.debug("ESPN scoreboard fetch failed (%s %s): %s", sport, date_str, exc)
        return None

    want_home, want_away = _norm(home_team), _norm(away_team)
    for ev in events:
        for comp in ev.get("competitions", []):
            status = (comp.get("status", {}).get("type", {}) or {})
            if not status.get("completed"):
                continue
            home = away = None
            for c in comp.get("competitors", []):
                abbr = _norm((c.get("team") or {}).get("abbreviation"))
                try:
                    score = int(c.get("score"))
                except (TypeError, ValueError):
                    score = None
                if c.get("homeAway") == "home":
                    home = (abbr, score)
                elif c.get("homeAway") == "away":
                    away = (abbr, score)
            if not home or not away:
                continue
            if home[1] is None or away[1] is None:
                continue
            # Match either orientation -- abbreviation styles can differ slightly
            # between our schedule provider and the scoreboard payload.
            if ((home[0] == want_home and away[0] == want_away)
                    or (home[0] == want_away and away[0] == want_home)):
                if home[0] == want_home:
                    return home[1], away[1]
                return away[1], home[1]
    return None
