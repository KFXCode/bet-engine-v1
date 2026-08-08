"""
output/history_log.py
=======================
Writes today's recommendations (picks + per-sport Best Parlay legs + the
cross-sport TOP parlay legs) into the recommendations table, tagged by sport,
and computes the rolling bankroll/P&L summary. Grading happens the NEXT run,
in backtest/grader.py -- today's picks start "pending".

Pre-lock change tracking: each time a pre-game re-run REPLACES the day's
picks, we diff the new set against the previous set and append a plain-English
note to data_store/pick_changes_<date>.json. run_daily reads these back so the
report can show what changed during the day before the slate locked.
"""

import json
from datetime import datetime, timezone

import config

LEDGER_CUTOFF = "2026-07-25"


def _changes_path(date_str):
    return config.DATA_STORE_DIR / f"pick_changes_{date_str}.json"


def get_pick_changes(date_str):
    p = _changes_path(date_str)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return []
    return []


def _save_pick_changes(date_str, changes):
    _changes_path(date_str).write_text(json.dumps(changes))


def _ml_labels(recs):
    return [r["team"] for r in recs if r["kind"] == "moneyline" and r.get("team")]


def _hr_labels(recs):
    return [r["side_or_player"] for r in recs if r["kind"] == "hr_prop"]


def _diff(old, new):
    old_s, new_s = set(old), set(new)
    return sorted(new_s - old_s), sorted(old_s - new_s)


def _phrase(kind_label, added, removed):
    bits = []
    if added:
        bits.append(f"now includes {', '.join(added)}")
    if removed:
        verb = "is" if len(removed) == 1 else "are"
        bits.append(f"{', '.join(removed)} {verb} no longer a pick")
    return f"{kind_label}: " + "; ".join(bits) + "."


def _record_changes(date_str, existing, plays, hr_props):
    if not existing:
        return
    old_ml, old_hr = _ml_labels(existing), _hr_labels(existing)
    new_ml = [p.team for p in plays]
    new_hr = [h["player_name"] for h in hr_props]

    parts = []
    add_ml, drop_ml = _diff(old_ml, new_ml)
    if add_ml or drop_ml:
        parts.append(_phrase("Moneyline", add_ml, drop_ml))
    add_hr, drop_hr = _diff(old_hr, new_hr)
    if add_hr or drop_hr:
        parts.append(_phrase("Home run", add_hr, drop_hr))

    if not parts:
        return
    changes = get_pick_changes(date_str)
    changes.append({
        "time": datetime.now(timezone.utc).astimezone().strftime("%-I:%M %p"),
        "text": " ".join(parts),
    })
    _save_pick_changes(date_str, changes)


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
        _record_changes(date_str, existing, plays, hr_props)
        # Pre-lock replace: wipe ALL of today's rows (not just pending). If an
        # in-progress game got a row graded earlier in the day, a pending-only
        # delete would leave it behind and the re-insert would DUPLICATE the
        # slate -- exactly what padded the HR ledger to 2-73. Pre-lock means
        # no legit result exists yet, so a full wipe of today is safe.
        with db.cursor() as cur:
            cur.execute("DELETE FROM recommendations WHERE date=?", (date_str,))

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

    for sport, par in sport_parlays.items():
        for leg in (par or {}).get("legs", []):
            db.insert_recommendation(
                date=date_str, game_id=None, kind="parlay_leg",
                side_or_player=leg["label"], team=None, sport=sport, odds_american=None,
                edge_pct=None, model_prob=None, market_prob=None,
                stake_units=0.0, stake_dollars=0.0, reasoning=[], factor_scores=[],
                created_at=now_iso,
            )

    if top_parlay and top_parlay.get("legs"):
        for leg in top_parlay["legs"]:
            db.insert_recommendation(
                date=date_str, game_id=None, kind="top_parlay_leg",
                side_or_player=leg["label"], team=None, sport="TOP", odds_american=None,
                edge_pct=None, model_prob=None, market_prob=None,
                stake_units=0.0, stake_dollars=0.0, reasoning=[], factor_scores=[],
                created_at=now_iso,
            )


def bankroll_summary(db, history_days=None):
    """Top-line records are the EXACT sum of the per-day history shown in the
    History tab (deduped + seed-pinned), so the big number can never drift
    from the day-by-day rows again. CLV still comes from the DB."""
    clv = db.get_clv_summary("moneyline")
    base = {
        "wins": 0, "losses": 0, "hr_wins": 0, "hr_losses": 0,
        "ml_since": None, "hr_since": None,
        "clv_n": clv["n"], "clv_avg": clv["avg_clv_pct"], "clv_beat": clv["beat_pct"],
        "units_net": 0.0, "dollars_net": 0.0, "running_bankroll": 0.0,
    }
    ml_dates, hr_dates = [], []
    for day in (history_days or []):
        for pick in day.get("picks", []):
            if pick.get("status") not in ("won", "lost"):
                continue
            won = pick["status"] == "won"
            if pick.get("kind") == "moneyline":
                base["wins" if won else "losses"] += 1
                ml_dates.append(day["date"])
            elif pick.get("kind") == "hr_prop":
                base["hr_wins" if won else "hr_losses"] += 1
                hr_dates.append(day["date"])
    base["ml_since"] = min(ml_dates) if ml_dates else None
    base["hr_since"] = min(hr_dates) if hr_dates else None

    history = db.get_bankroll_history(limit=10000)
    if history:
        base["units_net"] = sum((h.get("units_won") or 0) - (h.get("units_staked") or 0) for h in history)
        base["dollars_net"] = sum((h.get("dollars_won") or 0) - (h.get("dollars_staked") or 0) for h in history)
        lb = history[0].get("running_bankroll")
        base["running_bankroll"] = lb if lb is not None else 0.0
    return base
