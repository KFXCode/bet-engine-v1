"""
data/prop_odds.py
==================
NFL player-prop lines and prices from The Odds API: passing yards, rushing
yards, receiving yards, receptions and passing TDs.

WHAT THIS RETURNS
    {(game_id, market, norm_player): {"line", "over", "under", "book"}}
Both sides of every line, because the board bets overs AND unders and the
model de-vigs across the pair. A market with only one side priced is dropped
-- without both prices there's no way to strip the vig, and comparing against
a raw implied number invents an edge that is really just the book's margin.

CREDIT DISCIPLINE (this is the expensive endpoint)
Player props are billed PER EVENT PER MARKET. Five markets across a 14-game
Sunday is ~70 credits per uncached refresh, and the board refreshes several
times a day. Three protections, all learned from burning 20,000 credits in
weeks on the baseball side:
  1. DISK CACHE per event, 4 hours -- re-runs inside a window cost nothing.
  2. HARD STOP on 401. When the quota dies we stop calling immediately rather
     than firing one doomed request per event for the rest of the run.
  3. STALE FALLBACK. If the quota is gone we serve the last cached prices with
     a loud warning, because a slightly old real price beats no board at all
     -- but we never invent one.

NO FABRICATED LINES, EVER. If a market isn't posted, that player simply gets
no prop. This is the same rule the moneyline side now follows: a fake price
produces a confident pick on a bet that doesn't exist, and then grades it.

FanDuel is preferred, with a fallback to any other US book, because FanDuel
posts player props late in the week -- FanDuel-only code returned an empty
board on Thursday and Friday even though every other book was up. The book
that supplied each price is carried through so the report can name it.
"""

import json
import logging
import re
import time
import unicodedata
from pathlib import Path

import requests

import config

logger = logging.getLogger("prop_odds")

EVENTS_URL = f"{config.ODDS_API_BASE_URL}/sports/americanfootball_nfl/events"
EVENT_ODDS_URL = f"{config.ODDS_API_BASE_URL}/sports/americanfootball_nfl/events/{{eid}}/odds"

BOOK_PREFERENCE = ["fanduel", "draftkings", "betmgm", "williamhill_us",
                   "betrivers", "espnbet", "fanatics"]
BOOK_LABELS = {
    "fanduel": "FanDuel", "draftkings": "DraftKings", "betmgm": "BetMGM",
    "williamhill_us": "Caesars", "betrivers": "BetRivers",
    "espnbet": "ESPN BET", "fanatics": "Fanatics",
}

CACHE_MINUTES = max(240, int(getattr(config, "ODDS_CACHE_MINUTES", 240) or 240))
_CACHE_DIR = Path(config.DATA_STORE_DIR) / "prop_odds_cache"

_QUOTA_DEAD = False


def norm_player(name):
    """Shared name key. The engine and the odds feed spell names differently
    (accents, Jr./III, punctuation), and a mismatch silently drops a prop."""
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    n = re.sub(r"[.\,']", "", n)
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


def _cache_path(name):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(name))
    return _CACHE_DIR / f"{safe}.json"


def _cache_read(name, allow_stale=False):
    p = _cache_path(name)
    if not p.exists():
        return None
    try:
        blob = json.loads(p.read_text())
        age = (time.time() - blob.get("cached_at", 0)) / 60.0
        if age > CACHE_MINUTES and not allow_stale:
            return None
        if age > CACHE_MINUTES:
            logger.warning("Serving STALE prop prices for %s (%.0f min old) -- the Odds API is "
                           "unavailable. Verify every line before betting.", name, age)
        return blob.get("data")
    except Exception:
        return None


def _cache_write(name, data):
    try:
        _cache_path(name).write_text(json.dumps({"cached_at": time.time(), "data": data}))
    except Exception as exc:
        logger.debug("prop cache write failed for %s: %s", name, exc)


def _event_map():
    """{(home_abbr, away_abbr): [event_id, ...]} for today's NFL slate.

    The feed occasionally returns the same matchup as two events (one empty),
    so every id is kept and tried in turn."""
    global _QUOTA_DEAD
    from data.teams_nfl import normalize_nfl_team

    cached = _cache_read("nfl_events")
    if cached is not None:
        return {tuple(k.split("|")): v for k, v in cached.items()}
    if _QUOTA_DEAD:
        stale = _cache_read("nfl_events", allow_stale=True)
        return {tuple(k.split("|")): v for k, v in stale.items()} if stale else {}

    try:
        resp = requests.get(EVENTS_URL, params={"apiKey": config.ODDS_API_KEY}, timeout=15)
        if resp.status_code == 401:
            _QUOTA_DEAD = True
            logger.error("Prop odds: 401 -- Odds API key rejected or out of credits. "
                         "No further prop calls this run.")
            stale = _cache_read("nfl_events", allow_stale=True)
            return {tuple(k.split("|")): v for k, v in stale.items()} if stale else {}
        resp.raise_for_status()
        events = resp.json()
    except Exception as exc:
        logger.warning("Prop odds: /events fetch failed (%s).", exc)
        stale = _cache_read("nfl_events", allow_stale=True)
        return {tuple(k.split("|")): v for k, v in stale.items()} if stale else {}

    out = {}
    for ev in events if isinstance(events, list) else []:
        home = normalize_nfl_team(ev.get("home_team", ""))
        away = normalize_nfl_team(ev.get("away_team", ""))
        out.setdefault((home, away), []).append(ev.get("id"))
    _cache_write("nfl_events", {"|".join(k): v for k, v in out.items()})
    return out


def _fetch_event_props(event_id, markets):
    """All five markets for one event, both sides per player.

    Returns {(market, norm_name): {"line", "over", "under", "book"}}."""
    global _QUOTA_DEAD

    cache_key = f"props_{event_id}"
    cached = _cache_read(cache_key)
    if cached is not None:
        logger.info("Prop odds: cache HIT for event %s -- 0 credits.", event_id)
        return {tuple(k.split("||")): v for k, v in cached.items()}
    if _QUOTA_DEAD:
        stale = _cache_read(cache_key, allow_stale=True)
        return {tuple(k.split("||")): v for k, v in stale.items()} if stale else {}

    try:
        resp = requests.get(
            EVENT_ODDS_URL.format(eid=event_id),
            params={"apiKey": config.ODDS_API_KEY, "regions": "us",
                    "markets": ",".join(markets), "oddsFormat": "american"},
            timeout=20)
        if resp.status_code == 401:
            _QUOTA_DEAD = True
            logger.error("Prop odds: 401 on event %s -- stopping all prop calls this run.", event_id)
            stale = _cache_read(cache_key, allow_stale=True)
            return {tuple(k.split("||")): v for k, v in stale.items()} if stale else {}
        if resp.status_code == 422:
            logger.debug("Prop odds: event %s has no prop markets posted yet.", event_id)
            return {}
        resp.raise_for_status()
        payload = resp.json()
        remaining = resp.headers.get("x-requests-remaining")
        if remaining is not None:
            logger.info("Prop odds: event %s fetched | credits remaining %s.", event_id, remaining)
    except Exception as exc:
        logger.warning("Prop odds: event %s fetch failed: %s", event_id, exc)
        stale = _cache_read(cache_key, allow_stale=True)
        return {tuple(k.split("||")): v for k, v in stale.items()} if stale else {}

    def rank(book_key):
        return BOOK_PREFERENCE.index(book_key) if book_key in BOOK_PREFERENCE else len(BOOK_PREFERENCE)

    # (market, player) -> {"line", "over", "under", "book", "_rank"}
    best = {}
    for bm in payload.get("bookmakers", []):
        book_key = bm.get("key")
        r = rank(book_key)
        label = BOOK_LABELS.get(book_key, book_key)
        for market in bm.get("markets", []):
            mkey = market.get("key")
            if mkey not in markets:
                continue
            for outcome in market.get("outcomes", []):
                player = norm_player(outcome.get("description") or outcome.get("participant") or "")
                side = str(outcome.get("name", "")).strip().lower()
                point = outcome.get("point")
                price = outcome.get("price")
                if not player or side not in ("over", "under") or point is None or price is None:
                    continue
                k = (mkey, player)
                cur = best.get(k)
                # A better-ranked book replaces the whole entry, so the two
                # sides of a line always come from the SAME book -- de-vigging
                # across two different books' prices is meaningless.
                if cur is None or r < cur["_rank"]:
                    best[k] = {"line": float(point), "over": None, "under": None,
                               "book": label, "_rank": r}
                    cur = best[k]
                elif r > cur["_rank"]:
                    continue
                cur[side] = int(price)

    # Both sides required -- see the module docstring.
    result = {}
    for k, v in best.items():
        if v["over"] is None or v["under"] is None:
            continue
        result[k] = {"line": v["line"], "over": v["over"],
                     "under": v["under"], "book": v["book"]}

    if result:
        _cache_write(cache_key, {"||".join(k): v for k, v in result.items()})
    return result


def fetch_player_prop_odds(games):
    """games: today's NFL Game objects (preseason already excluded upstream).

    Returns {(game_id, market, norm_name): {"line", "over", "under", "book"}}."""
    if config.ODDS_MODE != "api" or not config.ODDS_API_KEY:
        logger.info("Prop odds: skipped (ODDS_MODE=%s, key set=%s).",
                    config.ODDS_MODE, bool(config.ODDS_API_KEY))
        return {}
    if not getattr(config, "PLAYER_PROPS_ENABLED", False):
        logger.info("Prop odds: player props disabled in config.")
        return {}

    nfl_games = [g for g in games
                 if g.sport == "NFL" and not getattr(g, "is_preseason", False)]
    if not nfl_games:
        return {}

    markets = config.PLAYER_PROP_MARKETS
    event_map = _event_map()
    if not event_map:
        logger.warning("Prop odds: no NFL events returned -- no player props today.")
        return {}

    out = {}
    for game in nfl_games:
        eids = event_map.get((game.home_team, game.away_team), [])
        if not eids:
            logger.warning("Prop odds: no Odds API event matched %s @ %s.",
                           game.away_team, game.home_team)
            continue
        props = {}
        for eid in eids:
            props = _fetch_event_props(eid, markets)
            if props:
                break
        for (market, player), price in props.items():
            out[(game.game_id, market, player)] = price
        logger.info("Prop odds: %s @ %s -> %d priced player-market line(s).",
                    game.away_team, game.home_team, len(props))

    logger.info("Prop odds: %d total priced lines across %d game(s).", len(out), len(nfl_games))
    return out
