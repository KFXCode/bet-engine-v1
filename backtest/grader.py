"""
backtest/grader.py
====================
Post-game review: fetch final scores for any game with pending recommendations,
mark each won/lost/push, and roll the result into bankroll_log. run_daily.py
calls this automatically at the start of every run.

ALL SPORTS: MLB settles through statsapi.mlb.com; every other league settles
through data/final_scores.py (ESPN scoreboard, multi-host) matched on the
stored game's date + team name, since our non-MLB ids are hashes.

BET TYPES:
  moneyline -> winner of the game
  hr_prop   -> data/lineups.get_hr_settled_players (MLB boxscore)
  td_prop   -> data/td_settle.get_td_scorers (NFL boxscore TD columns)
  total     -> final combined score vs the line stored on the pick. An exact
               landing (line 52, game totals 52) is a PUSH, not a loss.

Every prop/total is gated on the game being FINAL. Without that gate an
in-progress game marks players who simply haven't scored YET as losses.

SELF-HEALING PURGE. Junk and retired rows are cleaned in CODE at the top of
every grading pass, not by hand. Hand-fixing the database never held: the
workflow commits its own copy of the DB every run, so a manual upload silently
loses the race with whatever the runner checked out. Three categories:

  1. PLACEHOLDER MATCHUPS -- ESPN publishes undecided future rounds with the
     team literally named "TBD". Those became picks that can never be graded
     and sat pending forever, dragging a league's record with them.
  2. IMPOSSIBLE TOTALS -- three NCAAF totals were graded WINS off lines of
     8.0/7.5, which are baseball run totals produced by the mock odds provider
     during a credit outage. Every football game clears 8 points, so the model
     "won" them automatically and the record was inflated by bets that were
     never real. Any football total under 28 points is removed.
  3. RETIRED SPORTS (Sep 4, 2026) -- WNBA is switched off. Removing it from
     ENABLED_SPORTS stops NEW picks, but its old rows still lived in the
     ledger, which kept a WNBA tab and record on the report. Retiring a sport
     should remove it from the product completely, so its rows are purged too.

CLV (Closing Line Value): when a moneyline pick is graded, compare the price we
recommended to the CLOSING line. Positive CLV = the market moved toward our
side after we picked it -- the most reliable single signal a pick was +EV.
"""

import logging
import re
import unicodedata
from datetime import datetime, timezone

import requests

import config
from data.lineups import get_hr_settled_players
from data.final_scores import get_final_score_espn
from data.td_settle import get_td_scorers

logger = logging.getLogger(__name__)

# Team names that mean "opponent not decided yet".
PLACEHOLDER_TEAMS = ("TBD", "TBA", "TO BE DETERMINED", "TO BE ANNOUNCED")

# A football total below this was never a real market line.
MIN_FOOTBALL_TOTAL = 28.0
FOOTBALL_SPORTS = ("NCAAF", "NFL")

# Sports switched off for good -- their history is removed from the ledger so
# no tab or record survives. config.RETIRED_SPORTS overrides this if set.
RETIRED_SPORTS = tuple(getattr(config, "RETIRED_SPORTS", ("WNBA",)))


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


def _parse_total_pick(side_or_player):
    """'over 54.5' / 'under 47' -> ('over', 54.5). None if unparseable."""
    if not side_or_player:
        return None
    m = re.match(r"\s*(over|under)\s+([0-9]+(?:\.[0-9]+)?)", str(side_or_player), re.I)
    if not m:
        return None
    return m.group(1).lower(), float(m.group(2))


def purge_ungradeable(db):
    """Delete rows that can never be settled honestly, plus rows belonging to
    a retired sport. Runs every pass so a fix can't be lost to the workflow's
    own DB commit."""
    removed = {"placeholder": 0, "bad_total": 0, "retired": 0}
    try:
        with db.cursor() as cur:
            cur.execute("SELECT id, sport, kind, team, side_or_player FROM recommendations")
            rows = cur.fetchall()

            doomed = []
            for r in rows:
                sport = (r["sport"] or "").strip().upper()
                team = (r["team"] or "").strip().upper()
                label = (r["side_or_player"] or "").strip()

                if sport in RETIRED_SPORTS:
                    doomed.append((r["id"], "retired"))
                    continue
                if team in PLACEHOLDER_TEAMS or label.upper().startswith("TBD "):
                    doomed.append((r["id"], "placeholder"))
                    continue
                if r["kind"] == "total" and sport in FOOTBALL_SPORTS:
                    parsed = _parse_total_pick(label)
                    if parsed and parsed[1] < MIN_FOOTBALL_TOTAL:
                        doomed.append((r["id"], "bad_total"))

            for rec_id, why in doomed:
                cur.execute("DELETE FROM recommendations WHERE id=?", (rec_id,))
                removed[why] += 1

            placeholders = ",".join("?" for _ in PLACEHOLDER_TEAMS)
            cur.execute(
                f"DELETE FROM games WHERE UPPER(TRIM(home_team)) IN ({placeholders}) "
                f"OR UPPER(TRIM(away_team)) IN ({placeholders})",
                PLACEHOLDER_TEAMS + PLACEHOLDER_TEAMS,
            )
    except Exception as exc:
        logger.warning("Purge of ungradeable rows failed (continuing): %s", exc)
        return {"placeholder": 0, "bad_total": 0, "retired": 0}

    if removed["placeholder"]:
        logger.info("Purge: removed %d placeholder (TBD) pick(s) -- those can never grade.",
                    removed["placeholder"])
    if removed["bad_total"]:
        logger.info("Purge: removed %d football total(s) with an impossible line (<%.0f pts) -- "
                    "simulated-odds artifacts that would fake a win.",
                    removed["bad_total"], MIN_FOOTBALL_TOTAL)
    if removed["retired"]:
        logger.info("Purge: removed %d pick(s) from retired sport(s) %s -- the league is off the "
                    "product, so its tab and record come off with it.",
                    removed["retired"], ", ".join(RETIRED_SPORTS))
    return removed


def grade_pending(db):
    purge_ungradeable(db)

    pending = db.get_pending_recommendations()
    if not pending:
        return {"graded": 0, "hr_graded": 0, "td_graded": 0, "totals_graded": 0}

    graded_count = 0
    hr_graded = 0
    td_graded = 0
    totals_graded = 0
    final_cache = {}
    hr_settlement_cache = {}
    td_settlement_cache = {}
    by_date = {}

    def _final(game_id, sport):
        """MLB via statsapi; everything else via the ESPN scoreboard."""
        if game_id in final_cache:
            return final_cache[game_id]
        result = None
        if sport and sport != "MLB":
            row = db.get_game(game_id)
            if row:
                result = get_final_score_espn(sport, row.get("date"),
                                              row.get("home_team"), row.get("away_team"))
            else:
                logger.debug("No stored game row for %s (%s) -- cannot settle.", game_id, sport)
        else:
            result = _get_final_score_mlb(game_id)
        final_cache[game_id] = result
        return result

    for rec in pending:
        sport = rec.get("sport") or "MLB"

        # ---- HR props (MLB) -------------------------------------------------
        if rec["kind"] == "hr_prop" and rec["game_id"]:
            if _final(rec["game_id"], sport) is None:
                continue
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

        # ---- Anytime-TD props (NFL) ----------------------------------------
        if rec["kind"] == "td_prop" and rec["game_id"]:
            if _final(rec["game_id"], sport) is None:
                continue
            if rec["game_id"] not in td_settlement_cache:
                row = db.get_game(rec["game_id"])
                td_settlement_cache[rec["game_id"]] = (
                    get_td_scorers(row.get("date"), row.get("home_team"), row.get("away_team"))
                    if row else None
                )
            scorers = td_settlement_cache[rec["game_id"]]
            if scorers is None:
                continue
            scorers_norm = {_norm_name(n) for n in scorers}
            status = "won" if _norm_name(rec["side_or_player"]) in scorers_norm else "lost"
            db.set_recommendation_status(rec["id"], status)
            td_graded += 1
            logger.info("TD prop graded %s: %s -> %s", rec["date"], rec["side_or_player"], status)
            continue

        # ---- Game totals (Over/Under) ---------------------------------------
        if rec["kind"] == "total" and rec["game_id"]:
            result = _final(rec["game_id"], sport)
            if result is None:
                continue
            parsed = _parse_total_pick(rec["side_or_player"])
            if not parsed:
                logger.warning("Total pick %s: could not parse '%s' -- leaving pending.",
                               rec["id"], rec["side_or_player"])
                continue
            side, line = parsed
            combined = result[0] + result[1]
            if combined == line:
                status = "push"
            elif side == "over":
                status = "won" if combined > line else "lost"
            else:
                status = "won" if combined < line else "lost"
            db.set_recommendation_status(rec["id"], status)
            totals_graded += 1
            logger.info("%s TOTAL graded %s: %s %s -> %s (final combined %s)",
                        sport, rec["date"], side.upper(), line, status, combined)

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
            continue

        # ---- Moneyline ------------------------------------------------------
        if rec["kind"] != "moneyline" or not rec["game_id"]:
            continue
        result = _final(rec["game_id"], sport)
        if result is None:
            continue

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
        logger.info("%s ML graded %s: %s %s -> %s",
                    sport, rec["date"], rec.get("team"), rec["side_or_player"], status)

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
        prior_bankroll = (prior[0]["running_bankroll"]
                          if prior and prior[0].get("running_bankroll") is not None
                          else config.STARTING_BANKROLL)
        net_dollars = totals["d_won"] - totals["d_staked"]
        db.upsert_bankroll_day(
            day, units_staked=totals["staked"], units_won=totals["won"],
            dollars_staked=totals["d_staked"], dollars_won=totals["d_won"],
            running_bankroll=prior_bankroll + net_dollars,
            bets_graded=totals["graded"], wins=totals["wins"],
        )

    logger.info("Grading pass complete: %d moneyline, %d HR, %d TD, %d totals settled.",
                graded_count, hr_graded, td_graded, totals_graded)
    return {"graded": graded_count, "hr_graded": hr_graded,
            "td_graded": td_graded, "totals_graded": totals_graded}


def _get_final_score_mlb(game_id):
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
        logger.debug("final score fetch failed for MLB game %s: %s", game_id, exc)
        return None


def _payout(odds_american, stake):
    if odds_american is None or stake is None:
        return 0.0
    odds_american = float(odds_american)
    if odds_american > 0:
        return stake * (1 + odds_american / 100.0)
    return stake * (1 + 100.0 / -odds_american)
