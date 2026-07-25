"""
backtest/grader.py
====================
Post-game review: fetch final scores for any game with pending moneyline
recommendations, mark each recommendation won/lost, and roll the result into
bankroll_log. run_daily.py calls this automatically at the start of each run
(it grades YESTERDAY's plays before making today's).

HR props are auto-graded here too, from the final boxscore (each batter's
batting.homeRuns via data/lineups.get_hr_settled_players): the pick is a WIN
if that player hit >=1 HR, else a loss. They're tracked as a SEPARATE record
from moneyline (different bet type, different hit-rate expectation) rather
than blended into one number.
"""

import logging
import re
import unicodedata
from datetime import datetime, timezone

import requests

import config
from data.lineups import get_hr_settled_players

logger = logging.getLogger(__name__)


def _norm_name(name):
    """Normalize a player name for reliable HR-settlement matching: strip
    accents, punctuation, and Jr/Sr/II/III suffixes, collapse to lowercase.
    Without this a boxscore 'Jose Ramirez' won't match a stored 'Jose Ramirez'
    that differs only by an accent, silently grading a real winner as a loss."""
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    n = n.lower()
    n = re.sub(r"[.\,']", "", n)
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


def grade_pending(db):
    pending = db.get_pending_recommendations()
    if not pending:
        return {"graded": 0, "hr_graded": 0}

    graded_count = 0
    hr_graded = 0
    hr_settlement_cache = {}   # game_id -> set(names who homered) | None
    by_date = {}
    for rec in pending:
        if rec["kind"] == "hr_prop" and rec["game_id"]:
            if rec["game_id"] not in hr_settlement_cache:
                hr_settlement_cache[rec["game_id"]] = get_hr_settled_players(rec["game_id"])
            homered = hr_settlement_cache[rec["game_id"]]
            if homered is None:
                continue  # game not final yet -- leave pending
            homered_norm = {_norm_name(n) for n in homered}
            status = "won" if _norm_name(rec["side_or_player"]) in homered_norm else "lost"
            db.set_recommendation_status(rec["id"], status)
            hr_graded += 1
            logger.info("HR prop graded %s: %s -> %s", rec["date"], rec["side_or_player"], status)
            continue

        if rec["kind"] != "moneyline" or not rec["game_id"]:
            continue
        result = _get_final_score(rec["game_id"])
        if result is None:
            continue
        home_score, away_score = result
        if home_score == away_score:
            status = "push"
        else:
            winner_side = "home" if home_score > away_score else "away"
            status = "won" if rec["side_or_player"] == winner_side else "lost"
        db.set_recommendation_status(rec["id"], status)
        db.record_result(rec["game_id"], home_score, away_score, datetime.now(timezone.utc).isoformat())
        graded_count += 1

        day = by_date.setdefault(rec["date"], {"staked": 0.0, "won": 0.0, "d_staked": 0.0,
                                                 "d_won": 0.0, "wins": 0, "graded": 0})
        day["staked"] += rec["stake_units"] or 0
        day["d_staked"] += rec["stake_dollars"] or 0
        day["graded"] += 1
        if status == "won":
            day["won"] += _payout(rec["odds_american"], rec["stake_units"])
            day["d_won"] += _payout(rec["odds_american"], rec["stake_dollars"])
            day["wins"] += 1
        elif status == "push":
            day["won"] += rec["stake_units"] or 0
            day["d_won"] += rec["stake_dollars"] or 0

    for day, totals in sorted(by_date.items()):  # chronological, so bankroll chains correctly
        prior = db.get_bankroll_history(limit=1)
        prior_bankroll = prior[0]["running_bankroll"] if prior and prior[0].get("running_bankroll") is not None else config.STARTING_BANKROLL
        net_dollars = totals["d_won"] - totals["d_staked"]
        db.upsert_bankroll_day(
            day, units_staked=totals["staked"], units_won=totals["won"],
            dollars_staked=totals["d_staked"], dollars_won=totals["d_won"],
            running_bankroll=prior_bankroll + net_dollars,
            bets_graded=totals["graded"], wins=totals["wins"],
        )

    return {"graded": graded_count, "hr_graded": hr_graded}


def _get_final_score(game_id):
    try:
        resp = requests.get(f"https://statsapi.mlb.com/api/v1.1/game/{game_id}/feed/live", timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        linescore = payload.get("liveData", {}).get("linescore", {})
        status = payload.get("gameData", {}).get("status", {}).get("abstractGameState")
        if status != "Final":
            return None
        home = linescore.get("teams", {}).get("home", {}).get("runs")
        away = linescore.get("teams", {}).get("away", {}).get("runs")
        if home is None or away is None:
            return None
        return home, away
    except Exception as exc:
        logger.debug("final score fetch failed for game %s: %s", game_id, exc)
        return None


def _payout(odds_american, stake):
    if odds_american is None or stake is None:
        return 0.0
    odds_american = float(odds_american)
    if odds_american > 0:
        return stake * (1 + odds_american / 100.0)
    return stake * (1 + 100.0 / -odds_american)
