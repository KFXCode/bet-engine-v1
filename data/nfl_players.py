"""
data/nfl_players.py
====================
Skill-position rosters and SEASON STAT PROFILES for NFL player props, from
free ESPN endpoints:

    roster -> /sports/football/nfl/teams/{espn_id}/roster
    stats  -> /common/v3/sports/football/nfl/athletes/{id}/stats

WHY PER-GAME RATES AND NOT TOTALS: a prop is a single-game question. A back
with 900 rush yards means nothing until you know whether that came in 6 games
or 16. Every field is stored as both the season total and the per-game
average, and models read the per-game number.

TWO ESPN QUIRKS THIS HANDLES:
  1. Stat rows come back for MANY seasons and NOT in a guaranteed order.
     Never trust statistics[0] -- we scan every row and keep the newest.
  2. Early in a season the current year has no data at all, so we fall back to
     the most recent completed season. Without that, every Week 1 prop would
     be modelled off zeros.

=====================================================================
ROSTER STATUS + ROLE FILTERING (Sep 14, 2026) -- the backup-QB bug.
=====================================================================
The prop boards were built from EVERY skill player on the roster, which is
wrong in two separate ways, and both shipped picks:

  1. NO STATUS FILTER. ESPN's roster includes Practice Squad, IR and
     day-to-day players. A practice-squad back cannot score a touchdown, but
     nothing stopped him being priced and published.

  2. NO ROLE FILTER. On KC@DEN the anytime-TD board published Patrick
     Mahomes AND Justin Fields -- Mahomes' backup. Fields is genuinely on the
     roster and genuinely Active, so no roster check could catch it; he
     simply will not take a snap while Mahomes is upright. Worse, the model
     built his scoring rate from his career as a STARTER elsewhere, so he
     graded as a strong +700 play and landed in the Top Parlay. A backup QB's
     true anytime-TD probability is near zero.

ESPN's depth-chart endpoint returns an empty body, so depth isn't directly
available. The reliable proxy is VOLUME: real contributors accumulate
attempts, carries and targets, and backups don't. So:

  - ONE QB PER TEAM. Quarterbacks are strictly ranked by passing volume and
    only the leader survives. Two QBs from one team can never both be
    starters, and it's the single highest-confidence cut available.
  - PER-POSITION VOLUME FLOORS. A player below the floor for his position
    isn't a prop candidate regardless of name value.

These are deliberately conservative: they remove players who demonstrably
don't carry a workload, and they do NOT try to guess a starter among two
genuine committee backs -- that's a real ambiguity the volume model should
price, not a filter should delete.
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

# Roster statuses that CANNOT produce a prop result. Anything else (Active,
# or an unknown label we haven't seen) is allowed through -- excluding an
# unrecognised status would silently shrink the board.
EXCLUDED_STATUSES = {
    "practice squad", "injured reserve", "ir", "out", "suspended",
    "physically unable to perform", "pup", "non football injury",
    "reserve/future", "waived", "released", "inactive",
}

# Minimum per-game volume to be considered a real contributor, by position.
# Below this a player is a depth piece whose prop is noise, not an edge.
VOLUME_FLOORS = {
    "QB": {"field": "pass_att_pg", "min": 10.0},
    "RB": {"field": "touch_pg", "min": 3.0},
    "FB": {"field": "touch_pg", "min": 1.0},
    "WR": {"field": "targets_pg", "min": 1.5},
    "TE": {"field": "targets_pg", "min": 1.0},
}

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


def _status_of(athlete):
    st = athlete.get("status") or {}
    for field in ("name", "type", "abbreviation", "description"):
        val = st.get(field)
        if val:
            return str(val)
    return "Active"


def get_skill_players(team_abbr):
    """[{player_id, name, position, status}] for skill players who could
    actually play. Practice squad / IR / out are filtered out -- see the
    module note."""
    tid = ESPN_TEAM_IDS.get(team_abbr)
    if not tid:
        logger.debug("No ESPN team id mapped for NFL abbr %s.", team_abbr)
        return []

    # Cache key is versioned so the status filter invalidates old cached
    # rosters that still contain practice-squad players.
    key = f"nfl_roster_v2:{team_abbr}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    payload = _get_json(ROSTER_HOSTS, tid=tid)
    if not payload:
        return []

    players = []
    excluded = []
    for group in payload.get("athletes", []):
        for a in group.get("items", []):
            pos = ((a.get("position") or {}).get("abbreviation") or "").upper()
            if pos not in SCORING_POSITIONS:
                continue
            pid = a.get("id")
            name = a.get("displayName")
            if not pid or not name:
                continue
            status = _status_of(a)
            if status.strip().lower() in EXCLUDED_STATUSES:
                excluded.append(f"{name} ({status})")
                continue
            players.append({"player_id": str(pid), "name": name,
                            "position": pos, "status": status})

    _cache_set(key, players)
    logger.info("NFL roster %s: %d available skill player(s)%s.",
                team_abbr, len(players),
                f", {len(excluded)} filtered out" if excluded else "")
    if excluded:
        logger.debug("NFL roster %s excluded: %s", team_abbr, ", ".join(excluded[:10]))
    return players


def filter_to_contributors(rosters_by_team, profiles):
    """Cut each roster down to players who plausibly carry a workload.

    Returns a NEW {team: [player, ...]} mapping. Two rules, both explained in
    the module note:
      1. one QB per team, the passing-volume leader
      2. per-position per-game volume floors

    Players with no profile at all are kept -- the prop models already skip
    them, and dropping them here would hide that in the logs."""
    out = {}
    for team, players in (rosters_by_team or {}).items():
        qbs = []
        others = []
        for p in players:
            prof = profiles.get(p["player_id"])
            if p["position"] == "QB":
                qbs.append((p, prof))
            else:
                others.append((p, prof))

        kept = []

        # --- QBs: strictly one, the volume leader -------------------------
        if qbs:
            def _pass_volume(pair):
                prof = pair[1] or {}
                return prof.get("pass_att_pg") or 0.0
            qbs.sort(key=_pass_volume, reverse=True)
            starter, starter_prof = qbs[0]
            benched = [p["name"] for p, _ in qbs[1:]]
            if (starter_prof or {}).get("pass_att_pg", 0) >= VOLUME_FLOORS["QB"]["min"]:
                kept.append(starter)
            else:
                benched.append(f"{starter['name']} (below QB volume floor)")
            if benched:
                logger.info("NFL %s: QB1 = %s; excluded %s -- a backup QB's anytime-TD and "
                            "passing props are near-zero equity, and his stat profile is from "
                            "starting elsewhere.", team, starter["name"], ", ".join(benched))

        # --- Everyone else: volume floor ----------------------------------
        for p, prof in others:
            if not prof:
                kept.append(p)
                continue
            floor = VOLUME_FLOORS.get(p["position"])
            if not floor:
                kept.append(p)
                continue
            field = floor["field"]
            if field == "touch_pg":
                value = (prof.get("rush_att_pg") or 0.0) + (prof.get("rec_pg") or 0.0)
            else:
                value = prof.get(field) or 0.0
            if value >= floor["min"]:
                kept.append(p)
            else:
                logger.debug("NFL %s: excluded %s (%s %.1f < floor %.1f).",
                             team, p["name"], field, value, floor["min"])

        dropped = len(players) - len(kept)
        out[team] = kept
        logger.info("NFL %s: %d contributor(s) kept, %d depth player(s) filtered.",
                    team, len(kept), dropped)
    return out


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
    None when the player has no usable stat history at all."""
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

    g = games if games > 0 else 1.0
    for base in ("pass_yds", "pass_att", "pass_cmp", "pass_td",
                 "rush_yds", "rush_att", "rush_td",
                 "rec_yds", "rec", "targets", "rec_td"):
        prof[f"{base}_pg"] = round(prof[base] / g, 3)
    prof["td_per_game"] = round(total_td / g, 3)
    prof["touch_pg"] = round((prof["rush_att"] + prof["rec"]) / g, 3)

    _cache_set(key, prof)
    return prof


def get_td_profile(player_id, name=None):
    """Back-compat shim for engine/td_props.py, which only needs the TD view."""
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
