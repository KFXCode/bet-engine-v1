"""
data/final_scores.py
=====================
Final scores for NON-MLB sports (WNBA, NFL, NCAAF, NCAAB, NHL, NBA), used by
backtest/grader.py to settle picks.

MULTI-HOST FIX (Aug 24, 2026): this module used to hit only site.api.espn.com
-- the one ESPN host blocked from GitHub Actions' datacenter IPs. From the
runner it returned nothing, the grader could never settle a WNBA/NFL pick, and
every one sat "pending" forever. It now goes through data/espn_fetch.py, which
tries site.api, site.web.api, AND the cdn.espn.com core feed.

NAME-SHAPE FIX (Sep 4, 2026): matching compared ESPN's ABBREVIATION against
our stored team name. That works for the pro leagues, whose abbreviations we
also store -- but NOT for college. NCAAF/NCAAB teams are stored as full
display names ("USC Trojans", "Michigan State Spartans") while ESPN's
abbreviation field says "USC" / "MSU", so a college game could never match and
EVERY NCAAF pick stayed pending -- no record at all, even for games that
finished days earlier.

Rather than special-casing college, each ESPN competitor now contributes a SET
of identifiers (abbreviation, displayName, shortDisplayName, location, name),
all normalized, and a match succeeds if our stored name equals ANY of them.
That is shape-agnostic: it works whether a sport is stored as an abbreviation
or a full name, so this class of bug can't come back when a new league is
added.

Our non-MLB game_ids are Odds API hashes, not ESPN event ids, so matching is
by DATE + TEAM NAME off the stored games row.

SPORT-AWARE ALIASES: abbreviation fixes cannot be global -- ESPN sends "LV"
for the WNBA Las Vegas Aces AND the NFL Raiders, "LA" for the WNBA Sparks and
the LA Rams. Each sport gets its own table. Confirmed mismatches this fixes:
    WNBA: ours WAS/GSV/LAS vs ESPN WSH/GS/LV
    NFL : ours WAS         vs ESPN WSH
"""

import logging
import re
import unicodedata

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

# Never try to settle a placeholder opponent.
UNSETTLEABLE = {"TBD", "TBA", ""}


def _norm(value):
    """Case/accent/punctuation-insensitive form, so 'San José State Spartans'
    and 'San Jose State Spartans' compare equal."""
    if not value:
        return ""
    s = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    s = s.upper()
    s = re.sub(r"[^A-Z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _canon(sport, value):
    """Alias-map an abbreviation, then normalize. Non-abbreviations pass
    through the alias step untouched and are just normalized."""
    raw = (value or "").strip().upper()
    mapped = ALIASES_BY_SPORT.get(sport, {}).get(raw, raw)
    return _norm(mapped)


def _identifiers(sport, team_block):
    """Every name shape ESPN offers for a team, normalized. Matching against
    the whole set is what makes abbreviation-stored and full-name-stored
    leagues both work."""
    t = team_block or {}
    ids = set()
    for field in ("abbreviation", "displayName", "shortDisplayName", "location", "name", "nickname"):
        val = t.get(field)
        if val:
            ids.add(_canon(sport, val))
    loc, name = t.get("location"), t.get("name")
    if loc and name:
        ids.add(_canon(sport, f"{loc} {name}"))
    ids.discard("")
    return ids


def _completed_games(sport, date_str):
    """[(home_ids, home_score, away_ids, away_score)] for every COMPLETED game
    that date. Each *_ids is a SET of normalized name shapes."""
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
                ids = _identifiers(sport, c.get("team"))
                try:
                    score = int(c.get("score"))
                except (TypeError, ValueError):
                    score = None
                if c.get("homeAway") == "home":
                    home = (ids, score)
                elif c.get("homeAway") == "away":
                    away = (ids, score)
            if home and away and home[1] is not None and away[1] is not None:
                out.append((home[0], home[1], away[0], away[1]))
    return out


def get_final_score_espn(sport, date_str, home_team, away_team):
    """(home_score, away_score) when the game is Final, else None."""
    want_home = _canon(sport, home_team)
    want_away = _canon(sport, away_team)

    # A "TBD" side means the schedule row was a placeholder -- there is no real
    # game to settle, so don't burn a fetch or log a scary mismatch for it.
    if want_home in UNSETTLEABLE or want_away in UNSETTLEABLE:
        logger.debug("%s %s: placeholder matchup (%s @ %s) -- nothing to settle.",
                     sport, date_str, away_team, home_team)
        return None

    completed = _completed_games(sport, date_str)
    if not completed:
        logger.debug("%s %s: no completed games returned by any ESPN host yet.", sport, date_str)
        return None

    # Exact pair, either orientation.
    for h_ids, hs, a_ids, as_ in completed:
        if want_home in h_ids and want_away in a_ids:
            return hs, as_
    for h_ids, hs, a_ids, as_ in completed:
        if want_home in a_ids and want_away in h_ids:
            return as_, hs

    # One side matched and the other name differs -- settle only if that team
    # played exactly one completed game that day, so it stays unambiguous.
    for want, is_home in ((want_home, True), (want_away, False)):
        hits = [g for g in completed if want in g[0] or want in g[2]]
        if len(hits) == 1:
            h_ids, hs, a_ids, as_ = hits[0]
            logger.info("%s %s: matched on %s only (ours %s @ %s) -- settling.",
                        sport, date_str, want, away_team, home_team)
            if want in h_ids:
                return (hs, as_) if is_home else (as_, hs)
            return (as_, hs) if is_home else (hs, as_)

    logger.warning("%s %s: could NOT match %s @ %s among %d completed game(s).",
                   sport, date_str, away_team, home_team, len(completed))
    return None
