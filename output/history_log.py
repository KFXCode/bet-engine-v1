"""
output/history_log.py
=======================
Writes today's recommendations into the recommendations table, tagged by sport,
and computes the rolling bankroll/P&L summary. Grading happens the NEXT run,
in backtest/grader.py -- today's picks start "pending".

WHAT GETS LOGGED:
  moneyline          -- the day's picks, each tagged with its sport
  parlay_leg         -- each sport's own Best Parlay legs
  top_parlay_leg     -- the cross-sport TOP parlay legs (sport='TOP')
  double_parlay_leg  -- the 2-leg "Double Your Money" ticket (sport='DOUBLE')

=====================================================================
A PUBLISHED PICK IS NEVER DELETED (Oct 3, 2026)
=====================================================================
The old rule was "latest pre-game run wins": every re-run before first pitch
wiped the day's rows and re-inserted whatever that run picked. That quietly
erased real bets. On Oct 1 the noon board published ATL ML -106 as the top
MLB play; a 6:19 PM run, still before the 8 PM first pitch, saw moved odds,
found nothing clearing the bar, and deleted ATL from the ledger. ATL won 6-2
and the win never reached History -- a pick members could see and bet,
recorded nowhere.

Once a pick has been shown, someone may have bet it, so it belongs in the
record. The rule is now APPEND-ONLY:
  - later runs may ADD picks that weren't on the board yet
  - they may NEVER remove or replace one that was already published
  - the day's tickets (Best / Top / Double) keep the FIRST version published,
    because those are the tickets people actually saw and played
  - at/after first pitch the day is fully locked, same as before

So the History tab is now the complete list of everything the engine ever put
in front of you, graded honestly. The live page can still show the latest
run's view; the ledger is the full record.
"""

import json
from datetime import datetime, timezone

import config

LEDGER_CUTOFF = "2026-07-25"

TICKET_KINDS = {"parlay_leg", "top_parlay_leg", "double_parlay_leg"}


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


def _note_additions(date_str, added):
    if not added:
        return
    changes = get_pick_changes(date_str)
    changes.append({
        "time": datetime.now(timezone.utc).astimezone().strftime("%-I:%M %p"),
        "text": f"Moneyline: added {', '.join(added)} (earlier picks stay on the record).",
    })
    _save_pick_changes(date_str, changes)


def _log_parlay_legs(db, date_str, parlay, kind, sport, now_iso):
    for leg in (parlay or {}).get("legs", []):
        db.insert_recommendation(
            date=date_str, game_id=None, kind=kind,
            side_or_player=leg.get("label", ""), team=None, sport=sport,
            odds_american=None, edge_pct=None, model_prob=None, market_prob=None,
            stake_units=0.0, stake_dollars=0.0, reasoning=[], factor_scores=[],
            created_at=now_iso,
        )


def _insert_play(db, date_str, play, now_iso):
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


def log_recommendations(db, date_str, plays, hr_props=None, top_parlay=None,
                        sport_parlays=None, double_parlay=None, first_pitch_utc=None):
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    sport_parlays = sport_parlays or {}

    existing = db.get_recommendations_for_date(date_str)

    if existing and first_pitch_utc:
        try:
            fp = datetime.fromisoformat(str(first_pitch_utc).replace("Z", "+00:00"))
            if now >= fp:
                return          # day is locked
        except Exception:
            return

    # Moneylines: append any NEW pick; never touch a published one.
    have = {(r.get("game_id"), r.get("side_or_player"))
            for r in existing if r["kind"] == "moneyline"}
    added = []
    for play in plays:
        key = (play.game.game_id, play.side)
        if key in have:
            continue
        _insert_play(db, date_str, play, now_iso)
        have.add(key)
        added.append(play.team)
    if existing:
        _note_additions(date_str, added)

    # Tickets: keep the FIRST version shown -- only write a ticket type that
    # hasn't been recorded today yet.
    have_kinds = {(r["kind"], r.get("sport")) for r in existing if r["kind"] in TICKET_KINDS}
    for sport, par in sport_parlays.items():
        if ("parlay_leg", sport) not in have_kinds:
            _log_parlay_legs(db, date_str, par, "parlay_leg", sport, now_iso)
    if ("top_parlay_leg", "TOP") not in have_kinds:
        _log_parlay_legs(db, date_str, top_parlay, "top_parlay_leg", "TOP", now_iso)
    if ("double_parlay_leg", "DOUBLE") not in have_kinds:
        _log_parlay_legs(db, date_str, double_parlay, "double_parlay_leg", "DOUBLE", now_iso)


def bankroll_summary(db, history_days=None):
    """Top-line records are the EXACT sum of the per-day history shown in the
    History tab, so the big number can never drift from the day-by-day rows."""
    clv = db.get_clv_summary("moneyline")
    base = {
        "wins": 0, "losses": 0, "hr_wins": 0, "hr_losses": 0,
        "ml_since": None, "hr_since": None,
        "clv_n": clv["n"], "clv_avg": clv["avg_clv_pct"], "clv_beat": clv["beat_pct"],
        "units_net": 0.0, "dollars_net": 0.0, "running_bankroll": 0.0,
    }
    ml_dates = []
    for day in (history_days or []):
        for pick in day.get("picks", []):
            if pick.get("status") not in ("won", "lost"):
                continue
            if pick.get("kind") == "moneyline":
                base["wins" if pick["status"] == "won" else "losses"] += 1
                ml_dates.append(day["date"])
    base["ml_since"] = min(ml_dates) if ml_dates else None

    history = db.get_bankroll_history(limit=10000)
    if history:
        base["units_net"] = sum((h.get("units_won") or 0) - (h.get("units_staked") or 0) for h in history)
        base["dollars_net"] = sum((h.get("dollars_won") or 0) - (h.get("dollars_staked") or 0) for h in history)
        lb = history[0].get("running_bankroll")
        base["running_bankroll"] = lb if lb is not None else 0.0
    return base
