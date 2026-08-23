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

MATCHING HARDENED (Aug 23, 2026): NFL picks were still not settling. Strict
"both abbreviations must match" matching is brittle -- our normalizers and
ESPN disagree on some codes (WAS/WSH, LAR/LA, GB/GNB, JAX/JAC), and NFL
PRESEASON games sit under a different seasontype on the scoreboard, so the
default query missed them entirely. Now:

  * every NFL request is tried across seasontype 1 (pre), 2 (regular),
    3 (post) so preseason settles;
  * abbreviations are canonicalized through an alias map before comparing;
  * a game matches if BOTH teams line up, or if ONE team lines up
    unambiguously (exactly one candidate game on that date has it);
  * when nothing matches, every completed game seen that day is logged, so
    the workflow log shows exactly which abbreviation pair to fix instead of
    failing silently.
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

# Sports whose season can be pre/regular/post at the same calendar date.
SEASON_TYPES = {
    "NFL": (1, 2, 3),
    "NCAAF": (1, 2, 3),
    "NBA": (1, 2, 3),
    "NHL": (1, 2, 3),
    "WNBA": (1, 2, 3),
    "NCAAB": (1, 2, 3),
}

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"

# Codes that differ between our normalizers and ESPN. Both sides are mapped
# through this before comparing, so either spelling matches.
ALIASES = {
    "WSH": "WAS", "LA": "LAR", "GNB": "GB", "JAC": "JAX", "KAN": "KC",
    "LVR": "LV", "NWE": "NE", "NOR": "NO", "SFO": "SF", "TAM": "TB",
    "ARZ": "ARI", "BLT": "BAL", "CLV": "CLE", "HST": "HOU",
    "PHO": "PHX", "NYL": "NY", "LVA": "LAS", "CONN": "CON", "GS": "GSV",
}


def _canon(abbr):
    a = (abbr or "").strip().upper()
    return ALIASES.get(a, a)


def _fetch_completed(sport, date_str):
    """Every COMPLETED game on that date: list of (home_abbr, home_score,
    away_abbr, away_score). Queries each relevant seasontype so preseason
    games aren't missed."""
    path = ESPN_PATHS.get(sport)
    if not path or not date_str:
        return []
    day = date_str.replace("-", "")
    out = []
    seen_ids = set()
    for stype in SEASON_TYPES.get(sport, (2,)):
        params = {"dates": day, "limit": 400, "seasontype": stype}
        try:
            resp = requests.get(SCOREBOARD.format(path=path), params=params, timeout=20)
            resp.raise_for_status()
            events = resp.json().get("events", [])
        except Exception as exc:
            logger.debug("ESPN scoreboard fetch failed (%s %s st=%s): %s", sport, date_str, stype, exc)
            continue
        for ev in events:
            if ev.get("id") in seen_ids:
                continue
            seen_ids.add(ev.get("id"))
            for comp in ev.get("competitions", []):
                status = (comp.get("status", {}).get("type", {}) or {})
                if not status.get("completed"):
                    continue
                home = away = None
                for c in comp.get("competitors", []):
                    abbr = _canon((c.get("team") or {}).get("abbreviation"))
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

    want_home, want_away = _canon(home_team), _canon(away_team)

    # 1. Exact pair, our orientation.
    for h_abbr, h_score, a_abbr, a_score in completed:
        if h_abbr == want_home and a_abbr == want_away:
            return h_score, a_score
    # 2. Exact pair, flipped (our home/away disagrees with ESPN's).
    for h_abbr, h_score, a_abbr, a_score in completed:
        if h_abbr == want_away and a_abbr == want_home:
            return a_score, h_score
    # 3. One side matches, and only ONE game that day involves it -- safe.
    for want, is_home in ((want_home, True), (want_away, False)):
        hits = [g for g in completed if want in (g[0], g[2])]
        if len(hits) == 1:
            h_abbr, h_score, a_abbr, a_score = hits[0]
            logger.info("%s %s: matched on one side (%s) -- other abbr differed "
                        "(ours %s/%s vs ESPN %s/%s). Settling anyway.",
                        sport, date_str, want, want_home, want_away, h_abbr, a_abbr)
            if want == h_abbr:
                return (h_score, a_score) if is_home else (a_score, h_score)
            return (a_score, h_score) if is_home else (h_score, a_score)

    logger.warning("%s %s: could NOT match %s @ %s. Completed games seen: %s",
                   sport, date_str, want_away, want_home,
                   ", ".join(f"{a}@{h}" for h, _, a, _ in completed) or "(none)")
    return None
