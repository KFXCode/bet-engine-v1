"""
data/hr_odds.py
================
"To hit a home run" (Over 0.5 HR) odds for the day's chosen HR props, via The
Odds API player-props endpoint.

Book policy (fixed Aug 14): PREFER FanDuel, but FALL BACK to any other US book
when FanDuel hasn't posted the market yet. FanDuel often posts HR props late
(only BetRivers/Caesars up hours before first pitch), and the old FanDuel-only
code returned "odds n/a" on everything in that window even though real prices
existed elsewhere. Now we show the best available price and label the book, so
you always see a real number; when it's not FanDuel, confirm on your app.

Details (confirmed against live API data):
  - Correct line is point == 0.5 ("to hit a HR"); never 1.5 (2+) / 2.5 (3+).
  - The Odds API can return the SAME game as TWO events (one empty). We keep
    ALL event ids per matchup and try each until one returns real prices.
  - Junk/placeholder prices (e.g. BetRivers +19900 = "not really offered") are
    filtered via HR_ODDS_MAX_PRICE.

Returns {(game_id, norm_name): {"odds": int, "book": str}}. Never raises.
"""

import logging
import re
import unicodedata

import requests

import config
from data.teams import normalize_team

logger = logging.getLogger(__name__)

# Preferred book order: FanDuel first, then common US books that post HR props.
BOOK_PREFERENCE = ["fanduel", "williamhill_us", "draftkings", "betmgm",
                   "betrivers", "espnbet", "fanatics"]
BOOK_LABELS = {
    "fanduel": "FanDuel", "williamhill_us": "Caesars", "draftkings": "DraftKings",
    "betmgm": "BetMGM", "betrivers": "BetRivers", "espnbet": "ESPN BET",
    "fanatics": "Fanatics",
}
# Anything longer than +2000 for a single-HR prop is a placeholder, not a real
# market (e.g. BetRivers returns +19900 for players it isn't truly pricing).
HR_ODDS_MAX_PRICE = 2000


def _norm_name(name):
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    n = re.sub(r"[.\,']", "", n)
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


def fetch_hr_odds(hr_props, games):
    """Returns {(game_id, norm_name): {"odds": int, "book": str}}.
    Prefers FanDuel, falls back to other US books."""
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
        books_seen = {v["book"] for v in odds_by_player.values()}
        logger.info("HR odds: %s@%s -> %d player price(s) from book(s): %s.",
                    game.away_team, game.home_team, len(odds_by_player),
                    ", ".join(sorted(books_seen)) or "none")
        players_here = [p for p in hr_props if p.get("game_id") == game_id]
        for pick in players_here:
            key = _norm_name(pick["player_name"])
            if key in odds_by_player:
                out[(game_id, key)] = odds_by_player[key]
            else:
                logger.info("HR odds: '%s' (norm '%s') not offered by any book: %s",
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
        out.setdefault((home, away), []).append(ev.get("id"))
    return out


def _is_hr_yes_line(outcome):
    side = str(outcome.get("name", "")).lower()
    if side not in ("over", "yes"):
        return False
    point = outcome.get("point")
    return point is None or abs(float(point) - 0.5) < 1e-6


def _fetch_event_hr_odds(event_id):
    """Fetch ALL US books' HR market, take only the 0.5 'to hit a HR' line, and
    keep the best-preference book's price per player. Returns
    {norm_name: {"odds": int, "book": str}}."""
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

    def pref_rank(book_key):
        return BOOK_PREFERENCE.index(book_key) if book_key in BOOK_PREFERENCE else len(BOOK_PREFERENCE)

    prices = {}  # norm_name -> {"odds", "book", "_rank"}
    for bm in payload.get("bookmakers", []):
        book_key = bm.get("key")
        rank = pref_rank(book_key)
        label = BOOK_LABELS.get(book_key, book_key)
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
                price = int(price)
                if abs(price) > HR_ODDS_MAX_PRICE:
                    continue  # junk/placeholder line
                cur = prices.get(player)
                if cur is None or rank < cur["_rank"]:
                    prices[player] = {"odds": price, "book": label, "_rank": rank}
    return {k: {"odds": v["odds"], "book": v["book"]} for k, v in prices.items()}
