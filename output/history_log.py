"""
output/history_log.py
=======================
Writes today's recommendations (picks + per-sport Best Parlay legs + the
cross-sport TOP parlay legs) into the recommendations table, tagged by sport,
and computes the rolling bankroll/P&L summary. Grading happens the NEXT run,
in backtest/grader.py -- today's picks start "pending".
"""

from datetime import datetime, timezone

LEDGER_CUTOFF = "2026-07-25"


def log_recommendations(db, date_str, plays, hr_props, top_parlay=None,
                        sport_parlays=None, first_pitch_utc=None):
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    sport_parlays = sport_parlays or {}

    # Slate-locking rule: before first pitch each re-run REPLACES the day's
    # picks (latest pre-game state wins); at/after first pitch it LOCKS.
    existing = db.get_recommendations_for_date(date_str)
    if existing:
        locked = True
        if first_pitch_utc:
            try:
                fp = datetime.fromisoformat(str(first_pitch_utc).replace("Z", "+00:00"))
                locked = now >= fp
            except Exception:
                locked = True
        if locked:
            return
        db.delete_pending_recommendations_for_date(date_str)

    for play in plays:
        db.insert_recommendation(
            date=date_str, game_id=play.game.game_id, kind="moneyline",
            side_or_player=play.side, team=play.team, sport=play.sport,
            odds_american=play.odds_american,
            edge_pct=play.edge_pct, model_prob=play.model_prob, market_prob=play.market_prob,
            stake_units=play.stake_units, stake_dollars=play.stake_dollars,
            reasoning=play.reasoning,
            factor_scores=[{"key": fs.key, "signal": fs.signal, "weight": fs.weight,
                            "reasoning": fs.reasoning, "data_quality": fs.data_quality}
                           for fs in play.factor_scores],
            created_at=now_iso,
        )
    for prop in hr_props:
        db.insert_recommendation(
            date=date_str, game_id=prop.get("game_id"), kind="hr_prop",
            side_or_player=prop["player_name"], team=prop["team"], sport="MLB",
            odds_american=prop.get("odds_american"),
            edge_pct=None, model_prob=None, market_prob=None,
            stake_units=1.0, stake_dollars=0.0, reasoning=prop["reasoning"], factor_scores=[],
            created_at=now_iso,
        )

    # Per-sport Best Parlay legs (kind='parlay_leg', tagged by that sport) --
    # so each sport's History shows the parlay it ran that day.
    for sport, par in sport_parlays.items():
        for leg in (par or {}).get("legs", []):
            db.insert_recommendation(
                date=date_str, game_id=None, kind="parlay_leg",
                side_or_player=leg["label"], team=None, sport=sport, odds_american=None,
                edge_pct=None, model_prob=None, market_prob=None,
                stake_units=0.0, stake_dollars=0.0, reasoning=[], factor_scores=[],
                created_at=now_iso,
            )

    # The cross-sport TOP parlay legs (kind='top_parlay_leg', sport='TOP').
    if top_parlay and top_parlay.get("legs"):
        for leg in top_parlay["legs"]:
            db.insert_recommendation(
                date=date_str, game_id=None, kind="top_parlay_leg",
                side_or_player=leg["label"], team=None, sport="TOP", odds_american=None,
                edge_pct=None, model_prob=None, market_prob=None,
                stake_units=0.0, stake_dollars=0.0, reasoning=[], factor_scores=[],
                created_at=now_iso,
            )


def bankroll_summary(db):
    history = db.get_bankroll_history(limit=10000)
    hr_record = db.get_record_by_kind("hr_prop", after=LEDGER_CUTOFF)
    ml_record = db.get_record_by_kind("moneyline", after=LEDGER_CUTOFF)
    clv = db.get_clv_summary("moneyline")

    # Verified results THROUGH LEDGER_CUTOFF (all MLB): cumulative ML 5-3, HR 3-3.
    SEED = {"ml_wins": 5, "ml_losses": 3, "hr_wins": 3, "hr_losses": 3, "since": "2026-07-24"}

    base = {
        "wins": ml_record["wins"] + SEED["ml_wins"], "losses": ml_record["losses"] + SEED["ml_losses"],
        "hr_wins": hr_record["wins"] + SEED["hr_wins"], "hr_losses": hr_record["losses"] + SEED["hr_losses"],
        "ml_since": SEED["since"], "hr_since": SEED["since"],
        "clv_n": clv["n"], "clv_avg": clv["avg_clv_pct"], "clv_beat": clv["beat_pct"],
        "units_net": 0.0, "dollars_net": 0.0, "running_bankroll": 0.0,
    }
    if not history:
        return base
    units_net = sum((h.get("units_won") or 0) - (h.get("units_staked") or 0) for h in history)
    dollars_net = sum((h.get("dollars_won") or 0) - (h.get("dollars_staked") or 0) for h in history)
    latest_bankroll = history[0].get("running_bankroll")
    base.update({
        "units_net": units_net, "dollars_net": dollars_net,
        "running_bankroll": latest_bankroll if latest_bankroll is not None else 0.0,
    })
    return base
