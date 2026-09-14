"""
backtest/grader.py
====================
Post-game review: settle every pending recommendation, roll results into
bankroll_log. run_daily.py calls this at the start of every run.

MLB settles via statsapi.mlb.com; every other league via data/final_scores.py
(ESPN scoreboard, multi-host) matched on date + team abbreviations.

BET TYPES
  moneyline    -> winner from the final score
  total        -> combined final score vs the stored line
  td_prop      -> data/td_settle.get_td_scorers (NFL boxscore TD columns)
  player_prop  -> data/player_settle (yards / receptions / pass TDs vs line)

Props are gated on the game being FINAL first. Without that gate an
in-progress game marks every player who simply hasn't produced YET as a loss,
which silently destroys the prop record.

DATE-WINDOW SETTLEMENT -- the bug that stranded 41 NFL picks. Non-MLB games
are matched to ESPN by DATE, and that date used to come straight from the
stored `games` row, which was the date of the RUN that discovered the game
rather than the day it was played. Saturday's run pulled the whole Sunday NFL
slate and stamped it Saturday, so every Sunday pick asked ESPN for Saturday's
scoreboard, found nothing final, and stayed pending forever. Nothing errored.
data/db.py now derives the stored date from the kickoff timestamp, and
settlement additionally tries several candidate dates:
    1. the recommendation's own date
    2. the stored game row's date
    3. one day either side of each (a 10pm ET kickoff is already "tomorrow"
       in UTC, which is how ESPN indexes some events)
The first candidate returning a FINAL score wins.

VOIDING A PLAYER WHO DIDN'T PLAY -- applies to BOTH prop boards now.
Sportsbooks void a player prop when the player doesn't take the field, so a
scratch must never count against the model: it had no way to predict a late
inactive, and recording it as a loss makes the tracked record worse than the
strategy actually was.

  - player_prop: grade_player_prop returns None when the player has no line
    in the box score. That used to mean "retry next run", so those picks sat
    pending forever. Now -> push.
  - td_prop: this is the subtler one, and it was WRONG until Sep 14. An
    anytime-TD prop grades by membership in the list of players who scored.
    An inactive player is simply absent from that list -- exactly like a
    player who suited up and didn't score -- so every scratch was silently
    graded a LOSS. Real case: James Conner didn't play on Sep 13 and the
    board read 6-4 when the honest result was 6-3.
    Settlement now checks PARTICIPATION first, via the same box score the
    player-prop path uses: no box-score line in a final game means inactive,
    which is a void. Only a player who actually played and failed to score
    takes the loss.

CLV: when a moneyline pick grades, compare the price we took to the CLOSING
line. Positive CLV means the market moved toward our side after we bet it.
"""

import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone

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


def _shift(date_str, days):
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=days)
        return d.strftime("%Y-%m-%d")
    except Exception:
        return None


def _candidate_dates(rec_date, row_date):
    """Dates to try, best guess first. See the DATE-WINDOW note above."""
    out = []
    for base in (rec_date, row_date):
        if not base:
            continue
        for delta in (0, 1, -1):
            d = _shift(base, delta)
            if d and d not in out:
                out.append(d)
    return out


def _played_in_game(stats, player_name):
    """Did this player appear in the final box score at all?

    True  -> he played (a TD prop miss is a real loss)
    False -> no line in a FINAL box score, i.e. inactive (void the prop)
    None  -> we can't tell, so the caller should leave the pick pending

    Written defensively about the shape of `stats` because guessing wrong here
    would mean voiding real losses, which flatters the record -- the opposite
    of the honesty this fix exists to protect. Anything unrecognised returns
    None rather than a confident answer."""
    if not stats:
        return None
    target = _norm_name(player_name)
    if not target:
        return None

    # Shape A: {normalized_name: {...stats...}}
    if isinstance(stats, dict):
        for key in stats.keys():
            if _norm_name(str(key)) == target:
                return True
        # A populated box score that doesn't contain him means he didn't play.
        return False if len(stats) > 0 else None

    # Shape B: iterable of records carrying a name field.
    if isinstance(stats, (list, tuple, set)):
        found_any = False
        for row in stats:
            found_any = True
            if isinstance(row, dict):
                for field in ("player", "name", "player_name", "athlete"):
                    if field in row and _norm_name(str(row[field])) == target:
                        return True
            elif _norm_name(str(row)) == target:
                return True
        return False if found_any else None

    return None


# "Bijan Robinson Over 68.5 Rushing Yards" -> name / side / line / market label
_PROP_RE = re.compile(r"^(?P<name>.+?)\s+(?P<side>Over|Under)\s+(?P<line>[\d.]+)\s+(?P<label>.+)$",
                      re.IGNORECASE)


def _parse_prop_label(label):
    """(name, market_key, side, line) or None. Handles the stored human format
    and the legacy pipe form, so nothing already on the ledger is stranded."""
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
        return {"graded": 0, "td_graded": 0, "totals_graded": 0,
                "props_graded": 0, "voided": 0}

    graded_count = 0
    td_graded = 0
    totals_graded = 0
    props_graded = 0
    voided = 0
    final_cache = {}      # game_id -> (scores, date_that_worked)
    td_cache = {}
    player_cache = {}
    by_date = {}
    unresolved = []

    def _final(game_id, sport, rec_date):
        """(home, away) final score plus the date it was found under, or
        (None, None). Tries several candidate dates -- see _candidate_dates."""
        if game_id in final_cache:
            return final_cache[game_id]

        result = None
        used_date = None

        if sport and sport != "MLB":
            row = db.get_game(game_id) or {}
            for candidate in _candidate_dates(rec_date, row.get("date")):
                found = get_final_score_espn(sport, candidate,
                                             row.get("home_team"), row.get("away_team"))
                if found:
                    result, used_date = found, candidate
                    if candidate != row.get("date"):
                        logger.info("Settled %s under %s (stored game row said %s) -- "
                                    "date drift handled.", game_id, candidate, row.get("date"))
                    break
            if result is None:
                unresolved.append(f"{sport} {game_id} ({row.get('away_team')}@{row.get('home_team')})")
        else:
            result = _get_final_score_mlb(game_id)
            used_date = rec_date

        final_cache[game_id] = (result, used_date)
        return result, used_date

    def _box_score(game_id, used_date, rec_date):
        """Per-player box score for a game, cached. Shared by both prop paths
        so the participation check and the yardage check agree."""
        if game_id not in player_cache:
            row = db.get_game(game_id) or {}
            player_cache[game_id] = get_player_stats(
                used_date or rec_date, row.get("home_team"), row.get("away_team"))
        return player_cache[game_id]

    for rec in pending:
        sport = rec.get("sport") or "MLB"
        kind = rec["kind"]
        rec_date = rec.get("date")

        # ---- Anytime-TD props (NFL) ----------------------------------------
        if kind == "td_prop" and rec["game_id"]:
            scores, used_date = _final(rec["game_id"], sport, rec_date)
            if scores is None:
                continue
            if rec["game_id"] not in td_cache:
                row = db.get_game(rec["game_id"]) or {}
                td_cache[rec["game_id"]] = get_td_scorers(
                    used_date or rec_date, row.get("home_team"), row.get("away_team"))
            scorers = td_cache[rec["game_id"]]
            if scorers is None:
                continue

            player = rec["side_or_player"]
            scorers_norm = {_norm_name(n) for n in scorers}
            if _norm_name(player) in scorers_norm:
                db.set_recommendation_status(rec["id"], "won")
                td_graded += 1
                logger.info("TD prop graded %s: %s -> won", rec_date, player)
                continue

            # He isn't on the scorers list. Before calling that a loss, check
            # he actually PLAYED -- an inactive player is absent for the same
            # reason, and books void those. This is the Conner case.
            played = _played_in_game(_box_score(rec["game_id"], used_date, rec_date), player)
            if played is None:
                logger.info("TD prop %s (%s): box score unreadable so participation is unknown "
                            "-- leaving pending rather than guessing.", rec_date, player)
                continue
            if played is False:
                db.set_recommendation_status(rec["id"], "push")
                voided += 1
                logger.info("TD prop VOIDED %s: %s never appeared in the final box score "
                            "(inactive) -- settled as a push, not a loss.", rec_date, player)
                continue

            db.set_recommendation_status(rec["id"], "lost")
            td_graded += 1
            logger.info("TD prop graded %s: %s -> lost (played, did not score)", rec_date, player)
            continue

        # ---- Player props (yards / receptions / pass TDs) ------------------
        if kind == "player_prop" and rec["game_id"]:
            parsed = _parse_prop_label(rec["side_or_player"])
            if not parsed:
                logger.warning("Player prop %s has an unparseable label -- skipping: %s",
                               rec["id"], rec["side_or_player"])
                continue
            scores, used_date = _final(rec["game_id"], sport, rec_date)
            if scores is None:
                continue
            stats = _box_score(rec["game_id"], used_date, rec_date)
            if stats is None:
                # Box score not readable yet -- genuinely retry next run.
                continue
            name, market, side, line = parsed
            status = grade_player_prop(stats, market, name, side, line)
            if status is None:
                # Final game, box score loaded, no line for this player: he
                # didn't play. Void it (see the module note).
                db.set_recommendation_status(rec["id"], "push")
                voided += 1
                logger.info("Player prop VOIDED %s: %s did not appear in the final box score "
                            "(inactive) -- settled as a push, excluded from the record.",
                            rec_date, name)
                continue
            db.set_recommendation_status(rec["id"], status)
            props_graded += 1
            logger.info("Player prop graded %s: %s %s %g (%s) -> %s",
                        rec_date, name, side, line, market, status)
            continue

        # ---- Totals (over/under on the game) -------------------------------
        if kind == "total" and rec["game_id"]:
            scores, _ = _final(rec["game_id"], sport, rec_date)
            if scores is None:
                continue
            home_score, away_score = scores
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
            logger.info("Total graded %s: %s (final %s) -> %s", rec_date, label, combined, status)
            continue

        # ---- Moneyline ------------------------------------------------------
        if kind != "moneyline" or not rec["game_id"]:
            continue
        scores, _ = _final(rec["game_id"], sport, rec_date)
        if scores is None:
            continue

        clv = _compute_clv(db, rec)
        if clv is not None:
            db.set_recommendation_clv(rec["id"], clv)

        home_score, away_score = scores
        if home_score == away_score:
            status = "push"
        else:
            winner_side = "home" if home_score > away_score else "away"
            status = "won" if rec["side_or_player"] == winner_side else "lost"
        db.set_recommendation_status(rec["id"], status)
        db.record_result(rec["game_id"], home_score, away_score, datetime.now(timezone.utc).isoformat())
        graded_count += 1
        logger.info("%s ML graded %s: %s %s -> %s",
                    sport, rec_date, rec.get("team"), rec["side_or_player"], status)

        day = by_date.setdefault(rec_date, {"staked": 0.0, "won": 0.0, "d_staked": 0.0,
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

    logger.info("Grading pass complete: %d ML, %d TD, %d totals, %d player props settled"
                "%s.", graded_count, td_graded, totals_graded, props_graded,
                f", {voided} voided (player inactive)" if voided else "")
    if unresolved:
        logger.warning("Could NOT find a final score for %d game(s) on any candidate date "
                       "(they stay pending and will retry next run): %s",
                       len(unresolved), ", ".join(sorted(set(unresolved))[:10]))
    return {"graded": graded_count, "td_graded": td_graded,
            "totals_graded": totals_graded, "props_graded": props_graded,
            "voided": voided}


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
