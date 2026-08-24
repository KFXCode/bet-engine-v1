"""
data/final_scores.py
=====================
Final scores for NON-MLB sports (WNBA, NFL, NCAAF, NCAAB, NHL, NBA), used by
backtest/grader.py to settle picks.

MULTI-HOST FIX (Aug 24, 2026): this module used to hit only
site.api.espn.com -- the one ESPN host that gets blocked from GitHub Actions'
datacenter IPs. From the runner it therefore returned nothing, the grader
could never settle a WNBA/NFL pick, and every one of them sat at "pending"
forever, so those leagues showed NO record no matter how many games had
finished. It now goes through data/espn_fetch.py, which tries site.api,
site.web.api, AND the cdn.espn.com core feed, so a block on one host can't
silently kill grading.

Our non-MLB game_ids are Odds API hashes, not ESPN event ids, so we match on
DATE + TEAM ABBREVIATIONS from the stored games row.

SPORT-AWARE ALIASES: abbreviation fixes cannot be global -- ESPN sends "LV"
for the WNBA Las Vegas Aces AND the NFL Raiders, "LA" for the WNBA Sparks and
the LA Rams. Each sport gets its own table, applied to BOTH sides before
comparing. Confirmed mismatches this fixes:
    WNBA: ours WAS/GSV/LAS vs ESPN WSH/GS/LV
    NFL : ours WAS         vs ESPN WSH
"""

import logging

from data.espn_fetch import fetch_scoreboard_events

logger = logging.getLogger(__name__)

ESPN_PATHS = {
    "WNBA": "basketball/wnba",
    "NBA": "basketball/nba",
    "NCAAB": "basketball/mens-college-basketball",
    "NFL": "football/nfl",
    "NCAAF": "football/college-football",
    "NHL": "hockey/nhl",
}

# Sweep pre/regular/post -- NFL preseason lives under seasontype 1, so asking
# for regular season only would miss every preseason result.
SEASON_TYPES = (None, 1, 2, 3)

ALIASES_BY_SPORT = {
    "WNBA": {
        "WSH": "WAS",
        "GS": "GSV", "GSW": "GSV",
        "LV": "LAS", "LVA": "LAS",
        "PHO": "PHX", "NYL": "NY", "CONN": "CON",
    },
    "NFL": {
        "WSH": "WAS", "LA": "LAR", "GNB": "GB", "JAC": "JAX", "KAN": "KC",
        "LVR": "LV", "OAK": "LV", "NWE": "NE", "NOR": "NO", "SFO": "SF",
        "TAM": "TB", "ARZ": "ARI", "BLT": "BAL", "CLV": "CLE", "HST": "HOU",
    },
    "NBA": {
        "WSH": "WAS", "GS": "GSW", "NY": "NYK", "SA": "SAS",
        "NO": "NOP", "PHO": "PHX", "UTAH": "UTA",
    },
    "NHL": {
        "WSH": "WAS", "TB": "TBL", "LA": "LAK", "SJ": "SJS",
        "NJ": "NJD", "VEG": "VGK",
    },
}


def _canon(sport, abbr):
    a = (abbr or "").strip().upper()
    return ALIASES_BY_SPORT.get(sport, {}).get(a, a)


def _completed_games(sport, date_str):
    """[(home, home_score, away, away_score)] for every COMPLETED game that
    date, abbreviations already canonicalized for this sport."""
    path = ESPN_PATHS.get(sport)
    if not path or not date_str:
        return []
    events = fetch_scoreboard_events(path, date_str, season_types=SEASON_TYPES)
    out = []
    for ev in events:
        for comp in ev.get("competitions", []):
            status = (comp.get("status", {}).get("type", {}) or {})
            if not status.get("completed"):
                continue
            home = away = None
            for c in comp.get("competitors", []):
                abbr = _canon(sport, (c.get("team") or {}).get("abbreviation"))
                try:
                    score = int(c.get("score"))
                except (TypeError, ValueError):
                    score = None
                if c.get("homeAway") == "home":
                    home = (abbr, score)
                elif c.get("homeAway") == "away":
                    away = (abbr, score)
            if home and away and home[1] is not None and away[1] is not None:
                out.append((home[0], home[1], away[0], away[1]))
    return out


def get_final_score_espn(sport, date_str, home_team, away_team):
    """(home_score, away_score) when the game is Final, else None."""
    completed = _completed_games(sport, date_str)
    if not completed:
        logger.debug("%s %s: no completed games returned by any ESPN host yet.", sport, date_str)
        return None

    want_home, want_away = _canon(sport, home_team), _canon(sport, away_team)

    for h, hs, a, as_ in completed:
        if h == want_home and a == want_away:
            return hs, as_
    for h, hs, a, as_ in completed:
        if h == want_away and a == want_home:
            return as_, hs
    # One side matched and the other abbreviation differs -- settle if that
    # single team played exactly one completed game that day (unambiguous).
    for want, is_home in ((want_home, True), (want_away, False)):
        hits = [g for g in completed if want in (g[0], g[2])]
        if len(hits) == 1:
            h, hs, a, as_ = hits[0]
            logger.info("%s %s: matched on %s only (ours %s/%s vs ESPN %s/%s) -- settling.",
                        sport, date_str, want, want_home, want_away, h, a)
            if want == h:
                return (hs, as_) if is_home else (as_, hs)
            return (as_, hs) if is_home else (hs, as_)

    logger.warning("%s %s: could NOT match %s @ %s. Completed that day: %s",
                   sport, date_str, want_away, want_home,
                   ", ".join(f"{a}@{h}" for h, _, a, _ in completed) or "(none)")
    return None
