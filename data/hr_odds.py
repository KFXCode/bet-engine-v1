"""
data/hr_odds.py
================
"To hit a home run" (Over 0.5 HR) odds for the day's chosen HR props, via The
Odds API player-props endpoint.

Three hard-won details (all confirmed against live API data):
  - The correct line is point == 0.5 ("to hit a HR"). The market also returns
    1.5 (2+ HRs) / 2.5 (3+ HRs) lines -- we must NOT grab those.
  - The Odds API frequently returns the SAME game as TWO events: one with
    bookmakers, one empty. Mapping matchup->single-id let the empty twin win,
    so odds came back n/a for everyone. We now keep ALL event ids per matchup
    and try each until one returns real prices.
  - FanDuel often hasn't posted HR props even when Caesars/BetRivers have, so
    FanDuel is preferred but we fall back to the best available US book.

Credit-conscious-ish: a matchup with a chosen HR pick may cost up to 2 event
calls (the duplicate), still only a handful of credits/day. Never raises.
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

    event_map = _event_id_map()   # (home, away) -> [event_id, ...]
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
                break  # first event id that actually has prices wins
        logger.info("HR odds: %s@%s -> %d player price(s) (tried %d event id(s)).",
                    game.away_team, game.home_team, len(odds_by_player), len(event_ids))
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
    """(home_abbr, away_abbr) -> LIST of event ids (the API can return the same
    matchup as multiple events, one of which may be empty)."""
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
    """True only for the 'to hit a HR' line: name Over/Yes AND point 0.5
    (or no point at all, for books that list it as a pure Yes/No)."""
    side = str(outcome.get("name", "")).lower()
    if side not in ("over", "yes"):
        return False
    point = outcome.get("point")
    return point is None or abs(float(point) - 0.5) < 1e-6


def _fetch_event_hr_odds(event_id):
    """Fetch every US book's HR market, take only the 0.5 line, and pick a
    price per player: FanDuel first, else the first other book that has it."""
    url = f"{config.ODDS_API_BASE_URL}/sports/baseball_mlb/events/{event_id}/odds"
    params = {
        "apiKey": config.ODDS_API_KEY,
        "regions": "us",
        "markets": config.ODDS_API_HR_MARKET,
        "oddsFormat": "american",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning("HR odds: event %s fetch failed: %s.", event_id, exc)
        return {}

    bookmakers = payload.get("bookmakers", [])
    if not bookmakers:
        return {}

    preferred = config.ODDS_API_BOOKMAKER
    fd_prices = {}
    fallback_prices = {}
    for bm in bookmakers:
        is_fd = bm.get("key") == preferred
        for market in bm.get("markets", []):
            if market.get("key") != config.ODDS_API_HR_MARKET:
                continue
            for outcome in market.get("outcomes", []):
                if not _is_hr_yes_line(outcome):
                    continue
                player = _norm_name(outcome.get("description") or outcome.get("participant") or "")
                price = outcome.get("price")
                if not player or price is None:
                    continue
                if is_fd:
                    fd_prices[player] = int(price)
                elif player not in fallback_prices:
                    fallback_prices[player] = int(price)

    out = dict(fallback_prices)
    out.update(fd_prices)
    return out
