"""
backtest/grader.py
====================
Post-game review: fetch final scores for any game with pending moneyline
recommendations, mark each recommendation won/lost, and roll the result into
bankroll_log. run_daily.py calls this automatically at the start of each run.

HR props are auto-graded here too, from the final boxscore.

CLV (Closing Line Value): when a moneyline pick is graded, we compare the
PRICE we recommended it at to the CLOSING line (the last odds snapshot
recorded for that game). Positive CLV = the market moved toward our side
after we picked it, i.e. we got a better number than the close -- the single
most reliable signal that a pick was genuinely +EV, independent of whether
it happened to win or lose. Stored per pick and summarized on the report.
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
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    n = n.lower()
    n = re.sub(r"[.\,']", "", n)
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


def _american_prob(ml):
    if ml is None:
        return None
    ml = float(ml)
    if ml > 0:
        return 100.0 / (ml + 100.0)
    return -ml / (-ml + 100.0)


def _compute_clv(db, rec):
    """CLV in probability points = closing implied prob (our side) minus the
    implied prob at our pick price. Positive => we beat the close."""
    pick_odds = rec.get("odds_american")
    if pick_odds is None:
        return None
    closing = db.get_latest_line(rec["game_id"])
    if not closing:
        return None
    side = rec["side_or_player"]
    closing_ml = closing["home_ml"] if side == "home" else closing["away_ml"]
    pick_p = _american_prob(pick_odds)
    close_p = _american_prob(closing_ml)
    if pick_p is None or close_p is None:
        return None
    return round((close_p - pick_p) * 100.0, 2)


def grade_pending(db):
    pending = db.get_pending_recommendations()
    if not pending:
        return {"graded": 0, "hr_graded": 0}

    graded_count = 0
    hr_graded = 0
    hr_settlement_cache = {}
    by_date = {}
    for rec in pending:
        if rec["kind"] == "hr_prop" and rec["game_id"]:
            if rec["game_id"] not in hr_settlement_cache:
                hr_settlement_cache[rec["game_id"]] = get_hr_settled_players(rec["game_id"])
            homered = hr_settlement_cache[rec["game_id"]]
            if homered is None:
                continue
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

        # CLV: record it whether the pick won or lost (that's the point).
        clv = _compute_clv(db, rec)
        if clv is not None:
            db.set_recommendation_clv(rec["id"], clv)

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

    for day, totals in sorted(by_date.items()):
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
