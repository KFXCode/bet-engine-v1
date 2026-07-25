"""
data/hr_odds.py
================
FanDuel "to hit a home run" (Over 0.5 HR / Yes) odds for the day's chosen HR
props, via The Odds API player-props endpoint.

Credit-conscious by design: player props are an EVENT-level market on The Odds
API (one request per game), which would burn the free tier fast if fetched for
every game on the slate. So fetch_hr_odds() only calls it for the handful of
games that actually contain a chosen HR pick (<= config.HR_PROP_MAX_PER_DAY),
after the picks are selected -- roughly 3 credits/day.

Flow:
  1. GET /events (free, 0 credits) -> map matchup -> Odds API event_id.
  2. For each game holding an HR pick, GET that event's odds for the
     batter_home_runs market, FanDuel only (1 credit each).
  3. Match each pick's player to the "Over"/"Yes" outcome, keep American odds.

Never raises: any failure just leaves that pick's odds as None (report shows
"odds n/a"), exactly like the rest of the pipeline degrades.
"""

import logging
import re
import unicodedata

import requests

import config
from data.teams import normalize_team

logger = logging.getLogger(__name__)


def _norm_name(name):
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    n = re.sub(r"[.\,']", "", n)
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


def fetch_hr_odds(hr_props, games):
    """Returns {(game_id, normalized_player_name): american_odds}. hr_props is
    the list of chosen HR pick dicts; games is today's Game list (to map a
    pick's game_id -> matchup -> Odds API event)."""
    if not (config.HR_ODDS_ENABLED and config.ODDS_MODE == "api" and config.ODDS_API_KEY and hr_props):
        return {}

    game_by_id = {g.game_id: g for g in games}
    needed_game_ids = {p.get("game_id") for p in hr_props if p.get("game_id") in game_by_id}
    if not needed_game_ids:
        return {}

    event_map = _event_id_map()
    if not event_map:
        return {}

    out = {}
    for game_id in needed_game_ids:
        game = game_by_id[game_id]
        event_id = event_map.get((game.home_team, game.away_team))
        if not event_id:
            continue
        odds_by_player = _fetch_event_hr_odds(event_id)
        players_here = [p for p in hr_props if p.get("game_id") == game_id]
        for pick in players_here:
            key = _norm_name(pick["player_name"])
            if key in odds_by_player:
                out[(game_id, key)] = odds_by_player[key]
    return out


def _event_id_map():
    """{(home_abbr, away_abbr): event_id} from the free /events endpoint."""
    url = f"{config.ODDS_API_BASE_URL}/sports/baseball_mlb/events"
    try:
        resp = requests.get(url, params={"apiKey": config.ODDS_API_KEY}, timeout=15)
        resp.raise_for_status()
        events = resp.json()
    except Exception as exc:
        logger.warning("HR odds: /events fetch failed (%s) -- skipping HR odds.", exc)
        return {}
    out = {}
    for ev in events:
        home = normalize_team(ev.get("home_team", ""))
        away = normalize_team(ev.get("away_team", ""))
        out[(home, away)] = ev.get("id")
    return out


def _fetch_event_hr_odds(event_id):
    """{normalized_player: american_odds} for the FanDuel batter_home_runs
    'Over/Yes' side of one event."""
    url = f"{config.ODDS_API_BASE_URL}/sports/baseball_mlb/events/{event_id}/odds"
    params = {
        "apiKey": config.ODDS_API_KEY,
        "regions": "us",
        "markets": config.ODDS_API_HR_MARKET,
        "bookmakers": config.ODDS_API_BOOKMAKER,
        "oddsFormat": "american",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.debug("HR odds: event %s fetch failed: %s", event_id, exc)
        return {}

    out = {}
    for bm in payload.get("bookmakers", []):
        if bm.get("key") != config.ODDS_API_BOOKMAKER:
            continue
        for market in bm.get("markets", []):
            if market.get("key") != config.ODDS_API_HR_MARKET:
                continue
            for outcome in market.get("outcomes", []):
                # 'Over' (point 0.5) or 'Yes' = the "hits a HR" side
                side = str(outcome.get("name", "")).lower()
                if side not in ("over", "yes"):
                    continue
                player = _norm_name(outcome.get("description") or outcome.get("participant") or "")
                if player and outcome.get("price") is not None:
                    out[player] = int(outcome["price"])
    return out
