"""
data/self_check.py
===================
Daily self-audit. Runs at the END of every pipeline and returns plain-English
warnings, which run_daily.py appends to data_warnings so they appear in the
"Data quality warnings" box at the top of the report.

WHY THIS EXISTS: nearly every function in this system catches its own errors
and degrades gracefully, so failures are SILENT -- the pipeline reports
success while quietly producing wrong output. Real examples that all
"succeeded" every run: a publish step that staged nothing for days, 41 NFL
picks that never graded because of a date mismatch, a Top Parlay that was
all-MLB for weeks, and a void check that turned 40 TD-prop losses into pushes.
None threw an exception. So this module asserts what SHOULD be true each day
and says so when it isn't.

Design rules:
  - NEVER raise. A broken self-check must not break the run it's auditing.
  - Report, don't repair. Warnings name the problem and where to look.
  - ONLY FIRE ON REAL ANOMALIES. A check that cries wolf gets ignored, and an
    ignored warning box is worse than none. That rule is why the parlay check
    below was rewritten on Sep 30: it flagged an all-MLB ticket on a day NHL
    was active but had no eligible leg -- an all-MLB ticket was the CORRECT
    output that day, and the warning trained you to stop reading the box.
"""

import logging
from datetime import datetime

import config

logger = logging.getLogger("self_check")

STALE_PENDING_HOURS = 48
IGNORED_KINDS = {"hr_prop", "parlay_leg", "double_parlay_leg", "top_parlay_leg"}

# Same wall engine/parlay.py uses: legs at or past this price are excluded,
# so a sport whose only plays are this chalky has nothing to contribute.
MAX_FAV = getattr(config, "ML_MAX_FAVORITE_PRICE", -200)

# A void rate above this on a prop board is almost certainly a grading bug,
# not a wave of scratches. Real inactive rates run in the single digits.
MAX_PLAUSIBLE_VOID_RATE = 0.25
MIN_PROPS_FOR_VOID_CHECK = 12


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
    for check in (_check_stuck_grading, _check_void_rate):
        try:
            warnings += check(db, today_str)
        except Exception as exc:
            logger.warning("Self-check %s failed (ignored): %s", check.__name__, exc)
    try:
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
    """Picks with no result long after their games finished."""
    pending = db.get_pending_recommendations()
    stale = {}
    for rec in pending:
        if rec.get("kind") in IGNORED_KINDS:
            continue
        if _days_ago(rec.get("date") or today_str, today_str) * 24 >= STALE_PENDING_HOURS:
            key = (rec.get("sport") or "?", rec.get("kind") or "?")
            stale[key] = stale.get(key, 0) + 1
    if not stale:
        return []
    parts = [f"{n} {sport} {kind}" for (sport, kind), n in sorted(stale.items())]
    return [f"GRADING STUCK: {sum(stale.values())} pick(s) are still ungraded more than "
            f"{STALE_PENDING_HOURS}h after their games ({'; '.join(parts)}). Check the "
            f"'Could NOT find a final score' lines in the workflow log."]


def _check_void_rate(db, today_str):
    """A prop board where an implausible share of picks voided. This is the
    check that would have caught the 40-push TD bug in its first week: real
    scratch rates are a few percent, not 77%."""
    out = []
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT kind, status, COUNT(*) AS c FROM recommendations "
                "WHERE kind IN ('td_prop','player_prop') AND date >= date(?, '-21 day') "
                "AND status IN ('won','lost','push') GROUP BY kind, status",
                (today_str,))
            rows = [dict(r) for r in cur.fetchall()]
    except Exception:
        return []
    by_kind = {}
    for r in rows:
        by_kind.setdefault(r["kind"], {})[r["status"]] = r["c"]
    for kind, counts in by_kind.items():
        total = sum(counts.values())
        pushes = counts.get("push", 0)
        if total >= MIN_PROPS_FOR_VOID_CHECK and pushes / total > MAX_PLAUSIBLE_VOID_RATE:
            out.append(f"VOID RATE: {pushes} of {total} graded {kind}s in the last 21 days "
                       f"settled as a push/void ({pushes * 100 // total}%). Real scratch rates "
                       f"are a few percent -- this almost certainly means the participation "
                       f"check is voiding real losses and the record is overstated.")
    return out


def _check_game_date_drift(db, today_str):
    try:
        rows = db.get_recommendations_for_date(today_str)
    except Exception:
        return []
    drift, seen = [], set()
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
            f"({', '.join(drift[:4])}{'...' if len(drift) > 4 else ''}). Grading tries a date "
            f"window so these still settle, but the stored dates are wrong."]


def _check_sports_with_no_picks(report, games):
    """A sport with games that produced neither a pick nor a stated reason."""
    sports_with_games = {g.sport for g in games}
    covered = {getattr(p, "sport", None) for p in (report.plays or [])}
    if getattr(report, "td_props", None) or getattr(report, "player_props", None):
        covered.add("NFL")
    if getattr(report, "totals", None):
        covered |= {t.get("sport") for t in report.totals}
    silent = sorted(s for s in sports_with_games if s and s not in covered)
    if not silent or report.dropped_notes:
        return []
    return [f"NO OUTPUT: {', '.join(silent)} had games today but produced no picks and no "
            f"'considered and dropped' notes. Either nothing cleared the edge bar (fine) or "
            f"that sport's odds/stats path is failing silently -- check the log for that league."]


def _check_simulated_odds(games, odds_by_game):
    mock = [g for g in games
            if odds_by_game.get(g.game_id) and odds_by_game[g.game_id].book == "mock"]
    if not mock:
        return []
    return [f"SIMULATED ODDS: {len(mock)} game(s) are priced with invented numbers, not a real "
            f"book. Any edge computed from them is meaningless. Usually means the Odds API is "
            f"out of credits -- check the-odds-api.com/account. Do not bet these."]


def _eligible_parlay_sports(report):
    """Sports that could actually contribute a Top Parlay leg today: a
    moneyline priced better than the heavy-chalk wall, or a priced NFL prop.
    Mirrors the eligibility rules in engine/parlay.build_daily_parlay."""
    sports = set()
    for p in (report.plays or []):
        odds = getattr(p, "odds_american", None)
        if odds is not None and odds > MAX_FAV:
            sports.add(getattr(p, "sport", None))
    for board in (getattr(report, "td_props", None), getattr(report, "player_props", None)):
        if any(c.get("odds_american") is not None for c in (board or [])):
            sports.add("NFL")
    sports.discard(None)
    return sports


def _check_parlay_diversity(report):
    """The Top Parlay should span sports -- but only when more than one sport
    actually HAD an eligible leg. Being 'active' isn't enough: an NHL slate
    whose only plays are -250 favourites has nothing to contribute, and an
    all-MLB ticket is then the correct output, not a bug."""
    legs = (getattr(report, "top_parlay", None) or {}).get("legs") or []
    if not legs:
        return []
    eligible = _eligible_parlay_sports(report)
    if len(eligible) < 2:
        return []
    leg_sports = {leg.get("sport") for leg in legs if leg.get("sport")}
    if len(leg_sports) >= 2:
        return []
    only = next(iter(leg_sports), "?")
    missing = sorted(eligible - leg_sports)
    return [f"TOP PARLAY is all-{only}, but {', '.join(missing)} also had eligible legs today. "
            f"It's meant to be the best ticket ACROSS sports -- the per-sport cap may not be "
            f"applied."]


def _check_prop_pricing(report):
    out = []
    for label, board in (("TD", getattr(report, "td_props", None)),
                          ("player", getattr(report, "player_props", None))):
        board = board or []
        if board and all(c.get("odds_american") is None for c in board):
            out.append(f"NO PRICES: all {len(board)} {label} prop(s) published without odds. "
                       f"Confirm each price on FanDuel before betting.")
    return out
