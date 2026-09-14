"""
data/self_check.py
===================
Daily self-audit. Runs at the END of every pipeline and returns a list of
plain-English warnings, which run_daily.py appends to data_warnings so they
appear in the "Data quality warnings" box at the top of the report.

WHY THIS EXISTS -- the honest version. Nearly every function in this system
catches its own errors and degrades gracefully so a single dead data source
can't kill the daily run. That was deliberate and it's the right default. But
the cost is that failures are SILENT: the pipeline reports success while
quietly producing wrong output, and the only detector is a human noticing days
later. Three real examples, all of which "succeeded" every single run:

  - The publish step staged `index.html` with an unmatched pathspec, staged
    nothing, printed "Nothing new to publish", and served a stale page for
    days.
  - 41 NFL picks never graded because the stored game rows carried the wrong
    date, so ESPN was asked about the wrong day and returned nothing.
  - The cross-sport TOP PARLAY silently came out all-MLB for weeks, because
    MLB's 15-game slate outnumbers the NFL's and legs were ranked on edge
    alone.

None of those threw an exception. So the fix isn't more try/except -- it's
asserting what SHOULD be true each day and shouting when it isn't. Each check
below maps to a failure that actually happened.

Design rules:
  - NEVER raise. A broken self-check must not break the run it's auditing.
  - Report, don't repair. Silent auto-fixes are how you get a system nobody
    understands; the warnings name the problem and where to look.
  - Only fire on real anomalies. A check that cries wolf gets ignored, and an
    ignored warning is worse than no warning.
"""

import logging
from datetime import datetime, timedelta

logger = logging.getLogger("self_check")

# A pick older than this with no result means grading is stuck, not pending.
STALE_PENDING_HOURS = 48

# Retired kinds never grade and must not be flagged as stuck.
IGNORED_KINDS = {"hr_prop", "parlay_leg", "double_parlay_leg", "top_parlay_leg"}


def _days_ago(date_str, today_str):
    try:
        a = datetime.strptime(date_str, "%Y-%m-%d")
        b = datetime.strptime(today_str, "%Y-%m-%d")
        return (b - a).days
    except Exception:
        return 0


def run_self_check(db, report, games, odds_by_game, today_str):
    """Returns list[str] of warnings. Never raises."""
    warnings = []
    try:
        warnings += _check_stuck_grading(db, today_str)
        warnings += _check_game_date_drift(db, today_str)
        warnings += _check_sports_with_no_picks(report, games)
        warnings += _check_simulated_odds(games, odds_by_game)
        warnings += _check_parlay_diversity(report)
        warnings += _check_prop_pricing(report)
    except Exception as exc:
        logger.warning("Self-check itself failed (ignored): %s", exc)

    if warnings:
        logger.warning("SELF-CHECK raised %d issue(s):", len(warnings))
        for w in warnings:
            logger.warning("SELF-CHECK: %s", w)
    else:
        logger.info("SELF-CHECK: no anomalies detected.")
    return warnings


def _check_stuck_grading(db, today_str):
    """Picks with no result long after their games finished. This is the check
    that would have caught the 41 stranded NFL picks on day one."""
    try:
        pending = db.get_pending_recommendations()
    except Exception as exc:
        return [f"Self-check couldn't read pending picks ({exc})."]

    stale = {}
    for rec in pending:
        if rec.get("kind") in IGNORED_KINDS:
            continue
        age_days = _days_ago(rec.get("date") or today_str, today_str)
        if age_days * 24 >= STALE_PENDING_HOURS:
            key = (rec.get("sport") or "?", rec.get("kind") or "?")
            stale[key] = stale.get(key, 0) + 1

    if not stale:
        return []

    parts = [f"{n} {sport} {kind}" for (sport, kind), n in sorted(stale.items())]
    total = sum(stale.values())
    return [f"GRADING STUCK: {total} pick(s) are still ungraded more than "
            f"{STALE_PENDING_HOURS}h after their games ({'; '.join(parts)}). "
            f"Results exist but settlement isn't finding them -- check the "
            f"'Could NOT find a final score' lines in the workflow log."]


def _check_game_date_drift(db, today_str):
    """A stored game row whose date disagrees with the picks made on it. This
    is the exact condition that stranded the NFL picks: upsert_game doesn't
    refresh `date` on conflict, so a game first seen a day early keeps the
    wrong date forever and grading asks ESPN about the wrong day."""
    try:
        rows = db.get_recommendations_for_date(today_str)
    except Exception:
        return []

    drift = []
    seen = set()
    for r in rows:
        gid = r.get("game_id")
        if not gid or gid in seen:
            continue
        seen.add(gid)
        try:
            game = db.get_game(gid) or {}
        except Exception:
            continue
        gdate = game.get("date")
        if gdate and gdate != today_str:
            drift.append(f"{gid} stored as {gdate}")

    if not drift:
        return []
    return [f"DATE DRIFT: {len(drift)} game(s) today are stored under a different date "
            f"({', '.join(drift[:4])}{'...' if len(drift) > 4 else ''}). Grading now tries a "
            f"date window so these still settle, but the stored dates are wrong."]


def _check_sports_with_no_picks(report, games):
    """A sport with games that produced neither a pick nor a stated reason.
    'No edge today' is a legitimate outcome -- silence is not, because that's
    what a broken schedule/odds path looks like from the outside."""
    sports_with_games = {g.sport for g in games}
    sports_with_plays = {getattr(p, "sport", None) for p in (report.plays or [])}
    prop_sports = set()
    if getattr(report, "td_props", None) or getattr(report, "player_props", None):
        prop_sports.add("NFL")
    if getattr(report, "totals", None):
        prop_sports |= {t.get("sport") for t in report.totals}

    covered = sports_with_plays | prop_sports
    silent = sorted(s for s in sports_with_games if s and s not in covered)
    if not silent:
        return []

    # Dropped notes ARE the explanation, so only warn when there are none.
    if report.dropped_notes:
        return []
    return [f"NO OUTPUT: {', '.join(silent)} had games today but produced no picks and no "
            f"'considered and dropped' notes. Either nothing cleared the edge bar (fine) or "
            f"that sport's odds/stats path is failing silently (not fine) -- check the log for "
            f"that league."]


def _check_simulated_odds(games, odds_by_game):
    """Any price that isn't from a real book. Betting a simulated number is
    strictly worse than not betting."""
    mock = [g for g in games
            if odds_by_game.get(g.game_id) and odds_by_game[g.game_id].book == "mock"]
    if not mock:
        return []
    return [f"SIMULATED ODDS: {len(mock)} game(s) are priced with invented numbers, not a real "
            f"book. Any edge computed from them is meaningless. Usually means the Odds API is "
            f"out of credits -- check the-odds-api.com/account. Do not bet these."]


def _check_parlay_diversity(report):
    """The TOP PARLAY claims to span every active sport. Verify it actually
    does when more than one sport is playing."""
    top = getattr(report, "top_parlay", None) or {}
    legs = top.get("legs") or []
    if not legs:
        return []
    active = [s for s in (report.active_sports or [])]
    if len(active) < 2:
        return []
    leg_sports = {leg.get("sport") for leg in legs if leg.get("sport")}
    if len(leg_sports) >= 2:
        return []
    only = next(iter(leg_sports), "?")
    return [f"TOP PARLAY is all-{only} even though {len(active)} sports are active "
            f"({', '.join(active)}). It's meant to be the best ticket ACROSS sports -- "
            f"the per-sport cap may not be applied."]


def _check_prop_pricing(report):
    """A whole prop board with no prices. The picks are still model-ranked, but
    without a price there's no edge to measure and no way to grade value."""
    out = []
    for label, board in (("TD", getattr(report, "td_props", None)),
                          ("player", getattr(report, "player_props", None))):
        board = board or []
        if not board:
            continue
        unpriced = [c for c in board if c.get("odds_american") is None]
        if len(unpriced) == len(board):
            out.append(f"NO PRICES: all {len(board)} {label} prop(s) published without odds. "
                       f"They're ranked on model probability only -- confirm each price on "
                       f"FanDuel before betting, and don't judge them on +EV.")
    return out
