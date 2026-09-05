"""
backtest/grader.py
====================
Post-game review: fetch final scores for any game with pending recommendations,
mark each won/lost/push, and roll the result into bankroll_log. run_daily.py
calls this automatically at the start of every run.

ALL SPORTS: MLB settles through statsapi.mlb.com; every other league settles
through data/final_scores.py (ESPN scoreboard, multi-host) matched on the
stored game's date + team abbreviations, since our non-MLB ids are hashes.

BET TYPES:
  moneyline    -> winner from the final score
  total        -> combined final score vs the stored line
  td_prop      -> data/td_settle.get_td_scorers (NFL boxscore TD columns)
  player_prop  -> data/player_settle (yards / receptions / pass TDs vs line)

Every prop is gated on the game actually being FINAL first. Without that gate
an in-progress game marks every player who simply hasn't produced YET as a
loss, which silently destroys the prop record.

PLAYER-PROP LABEL PARSING (Sep 5, 2026): player props are stored with the
human-readable label engine/player_props.label_for() produces --
"Bijan Robinson Over 68.5 Rushing Yards" -- because that same string is what
shows in the History tab. An earlier version of this grader expected a
pipe-delimited "Name|market|side|line" instead, so EVERY player prop failed to
parse and silently stayed pending forever: a whole board that published picks
and never once graded them. The parser below reads the real format, using
player_props.MARKET_BY_LABEL as the reverse lookup from "Rushing Yards" to
player_rush_yds, and it accepts the pipe form too so nothing already on the
ledger is stranded.

MLB is moneyline-only -- HR props are retired, so nothing here settles them.

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
from data.final_scores import get_final_score_espn
from data.td_settle import get_td_scorers
from data.player_settle import get_player_stats, grade_player_prop
from engine.player_props import MARKET_BY_LABEL

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


# "Bijan Robinson Over 68.5 Rushing Yards" -> name / side / line / market label
_PROP_RE = re.compile(r"^(?P<name>.+?)\s+(?P<side>Over|Under)\s+(?P<line>[\d.]+)\s+(?P<label>.+)$",
                      re.IGNORECASE)


def _parse_prop_label(label):
    """(name, market_key, side, line) or None.

    Handles the real stored format first, then the legacy pipe form so any
    rows written by the earlier build still settle."""
    if not label:
        return None

    if "|" in label:
        parts = label.split("|")
        if len(parts) == 4:
            name, market, side, line = parts
            try:
                return name, market, side.lower(), float(line)
            except (TypeError, ValueError):
                return None
        return None

    m = _PROP_RE.match(label.strip())
    if not m:
        return None
    market = MARKET_BY_LABEL.get(m.group("label").strip().lower())
    if not market:
        logger.warning("Player prop label has an unknown market: %r", m.group("label"))
        return None
    try:
        line = float(m.group("line"))
    except (TypeError, ValueError):
        return None
    return m.group("name").strip(), market, m.group("side").lower(), line


def grade_pending(db):
    pending = db.get_pending_recommendations()
    if not pending:
        return {"graded": 0, "td_graded": 0, "totals_graded": 0, "props_graded": 0}

    graded_count = 0
    td_graded = 0
    totals_graded = 0
    props_graded = 0
    final_cache = {}
    td_cache = {}
    player_cache = {}
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
        kind = rec["kind"]

        # ---- Anytime-TD props (NFL) ----------------------------------------
        if kind == "td_prop" and rec["game_id"]:
            if _final(rec["game_id"], sport) is None:
                continue
            if rec["game_id"] not in td_cache:
                row = db.get_game(rec["game_id"])
                td_cache[rec["game_id"]] = (
                    get_td_scorers(row.get("date"), row.get("home_team"), row.get("away_team"))
                    if row else None
                )
            scorers = td_cache[rec["game_id"]]
            if scorers is None:
                continue
            scorers_norm = {_norm_name(n) for n in scorers}
            status = "won" if _norm_name(rec["side_or_player"]) in scorers_norm else "lost"
            db.set_recommendation_status(rec["id"], status)
            td_graded += 1
            logger.info("TD prop graded %s: %s -> %s", rec["date"], rec["side_or_player"], status)
            continue

        # ---- Player props (yards / receptions / pass TDs) ------------------
        if kind == "player_prop" and rec["game_id"]:
            parsed = _parse_prop_label(rec["side_or_player"])
            if not parsed:
                logger.warning("Player prop %s has an unparseable label -- skipping: %s",
                               rec["id"], rec["side_or_player"])
                continue
            if _final(rec["game_id"], sport) is None:
                continue
            if rec["game_id"] not in player_cache:
                row = db.get_game(rec["game_id"])
                player_cache[rec["game_id"]] = (
                    get_player_stats(row.get("date"), row.get("home_team"), row.get("away_team"))
                    if row else None
                )
            stats = player_cache[rec["game_id"]]
            if stats is None:
                continue
            name, market, side, line = parsed
            status = grade_player_prop(stats, market, name, side, line)
            if status is None:
                continue
            db.set_recommendation_status(rec["id"], status)
            props_graded += 1
            logger.info("Player prop graded %s: %s %s %g (%s) -> %s",
                        rec["date"], name, side, line, market, status)
            continue

        # ---- Totals (over/under on the game) -------------------------------
        if kind == "total" and rec["game_id"]:
            result = _final(rec["game_id"], sport)
            if result is None:
                continue
            home_score, away_score = result
            combined = home_score + away_score
            label = rec["side_or_player"] or ""
            m = re.search(r"(over|under)\s+([\d.]+)", label, re.I)
            if not m:
                logger.warning("Total %s has no parseable line in '%s' -- skipping.", rec["id"], label)
                continue
            side = m.group(1).lower()
            try:
                line = float(m.group(2))
            except ValueError:
                continue
            if abs(combined - line) < 1e-9:
                status = "push"
            elif side == "over":
                status = "won" if combined > line else "lost"
            else:
                status = "won" if combined < line else "lost"
            db.set_recommendation_status(rec["id"], status)
            totals_graded += 1
            logger.info("Total graded %s: %s (final %s) -> %s", rec["date"], label, combined, status)
            continue

        # ---- Moneyline ------------------------------------------------------
        if kind != "moneyline" or not rec["game_id"]:
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

    logger.info("Grading pass complete: %d ML, %d TD, %d totals, %d player props settled.",
                graded_count, td_graded, totals_graded, props_graded)
    return {"graded": graded_count, "td_graded": td_graded,
            "totals_graded": totals_graded, "props_graded": props_graded}


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
