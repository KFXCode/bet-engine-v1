"""
data/hr_odds.py
================
"To hit a home run" (Over 0.5 HR) odds for the day's chosen HR props, via The
Odds API player-props endpoint -- FANDUEL ONLY (you bet FanDuel).

Details (confirmed against live API data):
  - The correct line is point == 0.5 ("to hit a HR"). We must NOT grab the
    1.5 (2+ HRs) / 2.5 (3+ HRs) lines.
  - The Odds API can return the SAME game as TWO events (one empty). We keep
    ALL event ids per matchup and try each until one returns real prices.
  - FanDuel ONLY: if FanDuel hasn't posted a player's HR prop, we return no
    price for him (odds n/a) rather than a different book's number. A player
    FanDuel doesn't offer usually isn't a confirmed starter anyway, so n/a is
    the honest signal -- don't bet a price your book isn't showing.

Never raises.
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
    """Returns {(game_id, normalized_player_name): american_odds} -- FanDuel only."""
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
    logger.info("HR odds: /events returned %d matchup(s).", len(event_map))
    if not event_map:
        return {}

    out = {}
    for game_id in needed_game_ids:
        game = game_by_id[game_id]
        event_ids = event_map.get((game.home_team, game.away_team), [])
        if not event_ids:
            logger.warning("HR odds: no Odds API event matched %s @ %s (keys like %s) -- team-abbr mismatch.",
                           game.away_team, game.home_team, list(event_map.keys())[:3])
            continue
        odds_by_player = {}
        for eid in event_ids:
            odds_by_player = _fetch_event_hr_odds(eid)
            if odds_by_player:
                break
        logger.info("HR odds: %s@%s -> %d FanDuel player price(s) (tried %d event id(s)).",
                    game.away_team, game.home_team, len(odds_by_player), len(event_ids))
        players_here = [p for p in hr_props if p.get("game_id") == game_id]
        for pick in players_here:
            key = _norm_name(pick["player_name"])
            if key in odds_by_player:
                out[(game_id, key)] = odds_by_player[key]
            else:
                logger.info("HR odds: '%s' (norm '%s') not offered on FanDuel: %s",
                            pick["player_name"], key, list(odds_by_player.keys())[:8])
    logger.info("HR odds: matched FanDuel prices for %d/%d pick(s).", len(out), len(hr_props))
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
        out.setdefault((home, away), []).append(ev.get("id"))
    return out


def _is_hr_yes_line(outcome):
    side = str(outcome.get("name", "")).lower()
    if side not in ("over", "yes"):
        return False
    point = outcome.get("point")
    return point is None or abs(float(point) - 0.5) < 1e-6


def _fetch_event_hr_odds(event_id):
    """Fetch FanDuel's HR market only, take only the 0.5 'to hit a HR' line."""
    url = f"{config.ODDS_API_BASE_URL}/sports/baseball_mlb/events/{event_id}/odds"
    params = {
        "apiKey": config.ODDS_API_KEY,
        "regions": "us",
        "markets": config.ODDS_API_HR_MARKET,
        "oddsFormat": "american",
        "bookmakers": config.ODDS_API_BOOKMAKER,
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning("HR odds: event %s fetch failed: %s.", event_id, exc)
        return {}

    fd_prices = {}
    for bm in payload.get("bookmakers", []):
        if bm.get("key") != config.ODDS_API_BOOKMAKER:
            continue
        for market in bm.get("markets", []):
            if market.get("key") != config.ODDS_API_HR_MARKET:
                continue
            for outcome in market.get("outcomes", []):
                if not _is_hr_yes_line(outcome):
                    continue
                player = _norm_name(outcome.get("description") or outcome.get("participant") or "")
                price = outcome.get("price")
                if player and price is not None:
                    fd_prices[player] = int(price)
    return fd_prices
