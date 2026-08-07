"""
data/hr_odds.py
================
FanDuel "to hit a home run" (Over 0.5 HR / Yes) odds for the day's chosen HR
props, via The Odds API player-props endpoint.

Credit-conscious: player props are an EVENT-level market (one request per
game), so fetch_hr_odds() only calls it for games that actually hold a chosen
HR pick (<= config.HR_PROP_MAX_PER_DAY) -- roughly 3 credits/day.

This version logs each step (event match, market presence, outcome count) so
that when odds come back "n/a" we can see exactly where it broke in the
workflow log instead of guessing. Never raises.
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
    """Returns {(game_id, normalized_player_name): american_odds}."""
    if not (config.HR_ODDS_ENABLED and config.ODDS_MODE == "api" and config.ODDS_API_KEY and hr_props):
        logger.info("HR odds: skipped (enabled=%s mode=%s key=%s props=%d)",
                    config.HR_ODDS_ENABLED, config.ODDS_MODE, bool(config.ODDS_API_KEY), len(hr_props or []))
        return {}

    game_by_id = {g.game_id: g for g in games}
    needed_game_ids = {p.get("game_id") for p in hr_props if p.get("game_id") in game_by_id}
    logger.info("HR odds: %d pick(s) across %d game(s) need odds.", len(hr_props), len(needed_game_ids))
    if not needed_game_ids:
        return {}

    event_map = _event_id_map()
    logger.info("HR odds: /events returned %d event(s).", len(event_map))
    if not event_map:
        return {}

    out = {}
    for game_id in needed_game_ids:
        game = game_by_id[game_id]
        event_id = event_map.get((game.home_team, game.away_team))
        if not event_id:
            logger.warning("HR odds: no Odds API event matched %s @ %s (keys like %s) -- check team-abbr mismatch.",
                           game.away_team, game.home_team,
                           list(event_map.keys())[:3])
            continue
        odds_by_player = _fetch_event_hr_odds(event_id)
        logger.info("HR odds: event %s (%s@%s) returned %d player price(s).",
                    event_id, game.away_team, game.home_team, len(odds_by_player))
        players_here = [p for p in hr_props if p.get("game_id") == game_id]
        for pick in players_here:
            key = _norm_name(pick["player_name"])
            if key in odds_by_player:
                out[(game_id, key)] = odds_by_player[key]
            else:
                logger.info("HR odds: '%s' (norm '%s') not among priced players: %s",
                            pick["player_name"], key, list(odds_by_player.keys())[:8])
    logger.info("HR odds: matched prices for %d/%d pick(s).", len(out), len(hr_props))
    return out


def _event_id_map():
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
        logger.warning("HR odds: event %s fetch failed: %s -- (a 422 here usually means the "
                       "batter_home_runs market or FanDuel isn't offered for this event yet).", event_id, exc)
        return {}

    bookmakers = payload.get("bookmakers", [])
    if not bookmakers:
        logger.info("HR odds: event %s returned NO bookmakers for market '%s' (props may not be posted yet, "
                    "or your plan/region doesn't include this market).", event_id, config.ODDS_API_HR_MARKET)
        return {}

    out = {}
    for bm in bookmakers:
        if bm.get("key") != config.ODDS_API_BOOKMAKER:
            continue
        for market in bm.get("markets", []):
            if market.get("key") != config.ODDS_API_HR_MARKET:
                continue
            for outcome in market.get("outcomes", []):
                side = str(outcome.get("name", "")).lower()
                if side not in ("over", "yes"):
                    continue
                player = _norm_name(outcome.get("description") or outcome.get("participant") or "")
                if player and outcome.get("price") is not None:
                    out[player] = int(outcome["price"])
    return out
