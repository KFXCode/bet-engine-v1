"""
data/final_scores.py
=====================
Final scores for NON-MLB sports (WNBA, NFL, NCAAF, NCAAB, NHL, NBA) from the
ESPN scoreboard -- the same source the schedule providers use.

Why this exists: backtest/grader.py graded every pick through
statsapi.mlb.com. A WNBA/NFL/etc game_id means nothing to that endpoint, so
those picks silently stayed "pending" forever and NEVER showed up in the
History tab.

Our non-MLB game_ids are Odds API hashes, not ESPN event ids, so we match on
DATE + TEAM ABBREVIATIONS from the stored games row.

SPORT-AWARE ALIASES (Aug 23, 2026): abbreviation fixes CANNOT be global,
because the same code means different teams in different leagues -- ESPN
sends "LV" for the WNBA Las Vegas Aces AND for the NFL Las Vegas Raiders,
and "LA" for the WNBA Sparks AND (historically) the LA Rams. A single shared
map therefore mis-settles one league while fixing another. Each sport now has
its own alias table, applied to BOTH our abbreviation and ESPN's before
comparing. Confirmed real mismatches this fixes:
    WNBA: ours WAS/GSV/LAS  vs ESPN WSH/GS/LV
    NFL : ours WAS          vs ESPN WSH

Matching order: exact pair -> flipped pair -> unambiguous single-team match.
When nothing matches, every completed game that day is logged so the workflow
log names the exact pair to add instead of failing silently.
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

# Sports whose season can be pre/regular/post at the same calendar date --
# NFL preseason lives under seasontype 1, so querying only 2 misses it.
SEASON_TYPES = {
    "NFL": (1, 2, 3),
    "NCAAF": (1, 2, 3),
    "NBA": (1, 2, 3),
    "NHL": (1, 2, 3),
    "WNBA": (1, 2, 3),
    "NCAAB": (1, 2, 3),
}

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"

# Per-sport canonical forms. Keys are any spelling we might see (ours or
# ESPN's); values are the one form both sides get normalized to.
ALIASES_BY_SPORT = {
    "WNBA": {
        "WSH": "WAS",                      # ESPN Mystics -> ours
        "GS": "GSV", "GSW": "GSV",         # ESPN Valkyries -> ours
        "LV": "LAS", "LVA": "LAS",         # ESPN Aces -> ours (NOT the Raiders)
        "PHO": "PHX", "NYL": "NY", "CONN": "CON",
        "POR": "POR", "TOR": "TOR", "LA": "LA",   # Sparks stay LA
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


def _fetch_completed(sport, date_str):
    """Every COMPLETED game that date: list of (home, home_score, away,
    away_score), abbreviations already canonicalized for this sport."""
    path = ESPN_PATHS.get(sport)
    if not path or not date_str:
        return []
    day = date_str.replace("-", "")
    out = []
    seen_ids = set()
    for stype in SEASON_TYPES.get(sport, (2,)):
        try:
            resp = requests.get(SCOREBOARD.format(path=path),
                                params={"dates": day, "limit": 400, "seasontype": stype},
                                timeout=20)
            resp.raise_for_status()
            events = resp.json().get("events", [])
        except Exception as exc:
            logger.debug("ESPN fetch failed (%s %s st=%s): %s", sport, date_str, stype, exc)
            continue
        for ev in events:
            if ev.get("id") in seen_ids:
                continue
            seen_ids.add(ev.get("id"))
            for comp in ev.get("competitions", []):
                if not (comp.get("status", {}).get("type", {}) or {}).get("completed"):
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
    """(home_score, away_score) when Final, else None."""
    completed = _fetch_completed(sport, date_str)
    if not completed:
        logger.debug("%s %s: no completed games on the ESPN scoreboard yet.", sport, date_str)
        return None

    want_home, want_away = _canon(sport, home_team), _canon(sport, away_team)

    for h, hs, a, as_ in completed:
        if h == want_home and a == want_away:
            return hs, as_
    for h, hs, a, as_ in completed:
        if h == want_away and a == want_home:
            return as_, hs
    for want, is_home in ((want_home, True), (want_away, False)):
        hits = [g for g in completed if want in (g[0], g[2])]
        if len(hits) == 1:
            h, hs, a, as_ = hits[0]
            logger.info("%s %s: matched on one side (%s); other abbr differed "
                        "(ours %s/%s vs ESPN %s/%s). Settling.",
                        sport, date_str, want, want_home, want_away, h, a)
            if want == h:
                return (hs, as_) if is_home else (as_, hs)
            return (as_, hs) if is_home else (hs, as_)

    logger.warning("%s %s: could NOT match %s @ %s. Completed that day: %s",
                   sport, date_str, want_away, want_home,
                   ", ".join(f"{a}@{h}" for h, _, a, _ in completed) or "(none)")
    return None
