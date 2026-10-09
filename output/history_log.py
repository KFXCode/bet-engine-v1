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

A PUBLISHED PICK IS NEVER DELETED (Oct 3, 2026). Later runs may ADD picks; they
may never remove or replace one that was already shown. The day's tickets keep
the FIRST version published. At/after first pitch the day is fully locked.

=====================================================================
NO PICK ON A GAME THAT ALREADY STARTED (Oct 9, 2026)
=====================================================================
On Oct 6, ESPN handed a Tuesday run the PREVIOUS weekend's finished NFL games.
The engine picked them -- with player stats that already included those games
-- and graded them instantly: a perfect 9-0 of bets nobody could have placed.
An audit found 43 such picks since July 26 (23 won, 19 lost, 1 push).

Two guards, both here so they run on every workflow run with no database
upload (uploads kept getting overwritten by the workflow's own DB commit):

  1. INSERT GUARD: log_recommendations refuses any moneyline whose game had
     already started at the moment of logging.
  2. LEDGER REPAIR: void_post_start_picks() voids every recommendation whose
     created_at is later than its game's start time. It runs at the start of
     every log_recommendations call, BEFORE History is built, and it's
     idempotent -- once a row is 'void' it's skipped. Voided rows aren't
     won/lost/push, so they drop out of History and every record.

data/espn_fetch.py also now drops other-day events at the source; these
guards are the backstop if any other data path ever does the same thing.
"""

import json
import logging
from datetime import datetime, timezone

import config

logger = logging.getLogger("history_log")

LEDGER_CUTOFF = "2026-07-25"

TICKET_KINDS = {"parlay_leg", "top_parlay_leg", "double_parlay_leg"}


def _parse_ts(ts):
    if not ts:
        return None
    try:
        s = str(ts).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def void_post_start_picks(db):
    """Void every recommendation created after its game started. Idempotent."""
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT r.id, r.created_at, g.game_time_utc FROM recommendations r "
                "JOIN games g ON r.game_id = g.game_id "
                "WHERE g.game_time_utc IS NOT NULL AND r.date > ? "
                "AND r.status IN ('won','lost','push','pending')", (LEDGER_CUTOFF,))
            rows = cur.fetchall()
            late = []
            for r in rows:
                created = _parse_ts(r["created_at"])
                start = _parse_ts(r["game_time_utc"])
                if created and start and created > start:
                    late.append(r["id"])
            for rid in late:
                cur.execute("UPDATE recommendations SET status='void' WHERE id=?", (rid,))
        if late:
            logger.warning("LEDGER REPAIR: voided %d pick(s) that were made after their game "
                           "had already started -- not bettable, removed from all records.",
                           len(late))
        return len(late)
    except Exception as exc:
        logger.warning("Post-start void pass skipped: %s", exc)
        return 0


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

    # Repair first, so the History built right after this call is clean.
    void_post_start_picks(db)

    existing = db.get_recommendations_for_date(date_str)

    if existing and first_pitch_utc:
        fp = _parse_ts(first_pitch_utc)
        if fp is None or now >= fp:
            return          # day is locked

    have = {(r.get("game_id"), r.get("side_or_player"))
            for r in existing if r["kind"] == "moneyline"}
    added = []
    for play in plays:
        start = _parse_ts(getattr(play.game, "game_time_utc", None))
        if start and now >= start:
            logger.info("Not logging %s: its game already started.", play.team)
            continue
        key = (play.game.game_id, play.side)
        if key in have:
            continue
        _insert_play(db, date_str, play, now_iso)
        have.add(key)
        added.append(play.team)
    if existing:
        _note_additions(date_str, added)

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
