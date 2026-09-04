"""
data/player_settle.py
======================
Actual player stat lines from a finished NFL game, for grading player props
(passing/rushing/receiving yards, receptions, QB pass TDs).

Source: ESPN's game summary endpoint
    /sports/football/nfl/summary?event=<espn_event_id>
whose boxscore carries per-player stat rows with labeled columns.

THE ID PROBLEM (same as data/td_settle.py): our NFL game_ids are Odds API
hashes, not ESPN event ids, so we locate the event by DATE + TEAM
ABBREVIATIONS off the scoreboard first, then pull its summary.

RETURN CONTRACT: None means "can't grade yet" -- game not final, box score
not posted, or a fetch failed -- and the caller MUST leave the pick pending.
A dict means the game is settled; a player missing from it did not record
that stat (0). Collapsing those two cases is how a prop board silently grades
everyone a loss on an in-progress game.

Yards can be negative (a sack loss, a run for -3), so 0 is a real value here,
never a stand-in for missing.
"""

import logging
import re
import unicodedata

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

NFL_ALIASES = {
    "WSH": "WAS", "LA": "LAR", "GNB": "GB", "JAC": "JAX", "KAN": "KC",
    "LVR": "LV", "OAK": "LV", "NWE": "NE", "NOR": "NO", "SFO": "SF",
    "TAM": "TB", "ARZ": "ARI", "BLT": "BAL", "CLV": "CLE", "HST": "HOU",
}

# Our market key -> (ESPN stat group, column label).
# ESPN's passing row reads "C/ATT" for completions/attempts and "TD" for
# passing touchdowns; rushing and receiving both use "YDS".
MARKET_SOURCES = {
    "player_pass_yds": ("passing", "YDS"),
    "player_pass_tds": ("passing", "TD"),
    "player_rush_yds": ("rushing", "YDS"),
    "player_reception_yds": ("receiving", "YDS"),
    "player_receptions": ("receiving", "REC"),
}


def _canon(abbr):
    return NFL_ALIASES.get((abbr or "").strip().upper(), (abbr or "").strip().upper())


def _norm_name(name):
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    n = re.sub(r"[.\,']", "", n)
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


def _num(v):
    try:
        return float(str(v).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


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
        logger.info("Player settle %s: matched %s @ %s on one side -- using event %s.",
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
            logger.debug("Player settle summary fetch failed (%s / %s): %s", base, event_id, exc)
    return None


def get_player_stats(date_str, home_team, away_team):
    """Returns {market_key: {normalized_player_name: value}} for a FINISHED
    game, or None when it can't be graded yet."""
    event_id = _find_event_id(date_str, home_team, away_team)
    if not event_id:
        logger.debug("Player settle: no completed ESPN event for %s @ %s on %s.",
                     away_team, home_team, date_str)
        return None

    payload = _fetch_summary(event_id)
    if not payload:
        return None

    teams = ((payload.get("boxscore") or {}).get("players") or [])
    if not teams:
        logger.debug("Player settle: summary %s has no player boxscore yet.", event_id)
        return None

    # group name -> {label -> column index}, plus the raw athlete rows
    out = {m: {} for m in MARKET_SOURCES}
    saw_any = False

    for team_block in teams:
        for group in team_block.get("statistics", []) or []:
            gname = group.get("name")
            labels = [str(l).upper() for l in (group.get("labels") or [])]
            if not labels:
                continue
            for market, (want_group, want_label) in MARKET_SOURCES.items():
                if gname != want_group or want_label not in labels:
                    continue
                idx = labels.index(want_label)
                for athlete in group.get("athletes", []) or []:
                    stats = athlete.get("stats") or []
                    if idx >= len(stats):
                        continue
                    name = ((athlete.get("athlete") or {}).get("displayName")
                            or (athlete.get("athlete") or {}).get("shortName"))
                    if not name:
                        continue
                    val = _num(stats[idx])
                    if val is None:
                        continue
                    saw_any = True
                    out[market][_norm_name(name)] = val

    if not saw_any:
        return None
    logger.info("Player settle %s (%s @ %s): %s",
                date_str, away_team, home_team,
                ", ".join(f"{m}={len(v)}" for m, v in out.items() if v) or "(no rows)")
    return out


def grade_player_prop(stats, market, player_name, side, line):
    """'won' | 'lost' | 'push' | None (ungradeable).

    A player absent from the box score for that market recorded none of it --
    0 receptions, 0 yards -- which is a legitimate UNDER win, not a missing
    value. Exact equality with the line is a PUSH (books refund it); most
    lines carry a .5 so this is rare but real on whole-number props."""
    if not stats or market not in stats:
        return None
    actual = stats[market].get(_norm_name(player_name), 0.0)
    try:
        line = float(line)
    except (TypeError, ValueError):
        return None
    if abs(actual - line) < 1e-9:
        return "push"
    if side == "over":
        return "won" if actual > line else "lost"
    if side == "under":
        return "won" if actual < line else "lost"
