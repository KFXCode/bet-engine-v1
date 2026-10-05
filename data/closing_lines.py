"""
data/closing_lines.py
======================
Captures a fresh price on every picked game shortly before it starts, so each
pick gets a real CLOSING LINE to compare against the price it was published at.

WHY: closing-line value is the clearest signal in the record so far -- picks
whose line moved TOWARD them won 74%, picks whose line moved AGAINST them won
37% (66 picks, Oct 5 grade). But only 66 of 266 priced picks could be measured,
because the engine recorded each game's price ONCE, at publish time. With no
later price, "closing line" just meant "the same price again" and CLV read as
zero. This module takes that missing second price.

NO EXTRA WORKFLOWS: the workflow already wakes every hour from 7 AM to 11 PM
ET, and after the day's board is published those runs stop at the auto-gate.
run_daily now hands those idle runs to capture_closing_lines() instead. For
each picked game starting within WINDOW_MINUTES, it pulls ONE fresh price and
stores it as an odds snapshot. Each game is captured once (marked in
stats_cache), so the cost is about one Odds API credit per sport per hourly
check that actually has a game about to start -- typically 5-15 credits a day.

The overnight grader already computes CLV from the LATEST snapshot
(backtest/grader._compute_clv), so once this snapshot exists, CLV becomes real
with no grader change.

CACHE BYPASS: the odds cache holds prices for 4 hours to save credits, which is
exactly wrong here -- it would hand back the publish-time price and call it
"closing." The capture temporarily sets the cache window to zero to force a
live fetch, then restores it. If the Odds API is out of credits, the fetch can
only return stale cached prices, so nothing is recorded rather than recording a
fake close.

Never raises -- a failed capture must not break the hourly check.
"""

import logging
from datetime import datetime, timezone

from engine.models import Game

logger = logging.getLogger("closing_lines")

# Capture games starting within this many minutes. Slightly over an hour so the
# hourly cron always catches each game once, even with GitHub's start delays.
WINDOW_MINUTES = 70

_MARKER = "closing:{gid}"


def _parse(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _already(db, gid):
    try:
        with db.cursor() as cur:
            cur.execute("SELECT 1 FROM stats_cache WHERE key=?", (_MARKER.format(gid=gid),))
            return cur.fetchone() is not None
    except Exception:
        return False


def _mark(db, gid):
    try:
        import time
        with db.cursor() as cur:
            cur.execute("INSERT OR REPLACE INTO stats_cache (key, payload, cached_at) VALUES (?, ?, ?)",
                        (_MARKER.format(gid=gid), "1", time.time()))
    except Exception as exc:
        logger.debug("Couldn't mark closing capture for %s: %s", gid, exc)


def _side_price(odds, side):
    return odds.home_ml if side == "home" else odds.away_ml


def _prob(ml):
    ml = float(ml)
    return 100.0 / (ml + 100.0) if ml > 0 else -ml / (-ml + 100.0)


def capture_closing_lines(db, date_str):
    """Snapshot a fresh price for each picked game starting soon. Returns the
    number of games captured."""
    from data import odds_providers as op

    try:
        picks = db.get_recommendations_for_date(date_str, kind="moneyline")
    except Exception as exc:
        logger.warning("Closing capture: couldn't read today's picks: %s", exc)
        return 0
    if not picks:
        return 0

    now = datetime.now(timezone.utc)
    due_by_sport = {}
    picks_by_game = {}
    for rec in picks:
        gid = rec.get("game_id")
        if not gid:
            continue
        picks_by_game.setdefault(gid, []).append(rec)
        if gid in {g.game_id for games in due_by_sport.values() for g in games}:
            continue
        if _already(db, gid):
            continue
        row = db.get_game(gid) or {}
        start = _parse(row.get("game_time_utc"))
        if not start:
            continue
        minutes_out = (start - now).total_seconds() / 60.0
        if not (0 < minutes_out <= WINDOW_MINUTES):
            continue
        sport = rec.get("sport") or "MLB"
        due_by_sport.setdefault(sport, []).append(Game(
            game_id=gid, date=row.get("date") or date_str,
            home_team=row.get("home_team"), away_team=row.get("away_team"),
            game_time_utc=row.get("game_time_utc"), sport=sport))

    if not due_by_sport:
        logger.info("Closing capture: no picked game starts within %d minutes.", WINDOW_MINUTES)
        return 0

    captured = 0
    now_iso = now.isoformat()
    saved_window = op.CACHE_MINUTES
    op.CACHE_MINUTES = 0          # force a live price, not the publish-time cache
    try:
        for sport, games in due_by_sport.items():
            try:
                fresh = op.get_odds_provider(sport).get_odds(games)
            except Exception as exc:
                logger.warning("Closing capture failed for %s: %s", sport, exc)
                continue
            if getattr(op, "_QUOTA_EXHAUSTED", False):
                logger.warning("Closing capture: Odds API out of credits -- not recording "
                               "stale prices as closing lines.")
                break
            for game in games:
                odds = fresh.get(game.game_id)
                if not odds or odds.book == "mock":
                    continue
                db.record_odds_snapshot(game.game_id, odds, now_iso, is_opening=False)
                _mark(db, game.game_id)
                captured += 1
                for rec in picks_by_game.get(game.game_id, []):
                    took = rec.get("odds_american")
                    close = _side_price(odds, rec.get("side_or_player"))
                    if took is None or close is None:
                        continue
                    move = (_prob(close) - _prob(took)) * 100.0
                    direction = ("MOVED YOUR WAY" if move > 0.05 else
                                 "moved against" if move < -0.05 else "unchanged")
                    logger.info("CLOSING %s %s: took %+d, now %+d -> %s (%+.1f pts)",
                                sport, rec.get("team"), took, close, direction, move)
    finally:
        op.CACHE_MINUTES = saved_window

    logger.info("Closing capture: stored a pre-game price for %d game(s).", captured)
    return captured
