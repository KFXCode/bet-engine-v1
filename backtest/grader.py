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

Props are gated on the game being FINAL first, so an in-progress game can't
mark every player who hasn't produced YET as a loss.

DATE-WINDOW SETTLEMENT: non-MLB games are matched to ESPN by date, and a
stored game row can carry the date of the RUN that found it rather than the
day it was played. Settlement tries the recommendation's date, the stored
row's date, and one day either side of each, taking the first FINAL result.

=====================================================================
THE VOID BUG (fixed Sep 30, 2026) -- read this before touching voids.
=====================================================================
On Sep 14 a participation check was added so a scratched player's TD prop
voids instead of grading as a loss. The check was WRONG, and wrong in the
worst direction: it flattered the record.

data/player_settle.get_player_stats returns
    {market_key: {normalized_player_name: value}}
-- keyed by MARKET first. The check treated it as keyed by PLAYER, compared
each player's name against "player_pass_yds", "player_rush_yds" and so on,
never found a match, and concluded that every player who didn't score had
not played. Result over two weeks: 12 TD props won, 0 lost, 40 "voided". A
flawless-looking record built entirely out of a lookup against the wrong
level of a dictionary.

_played_in_game now looks in EVERY market's player map. A player who logged
any passing, rushing or receiving line in the final box score played.

KNOWN LIMIT, stated honestly: a player who was active but recorded no
passing, rushing or receiving stat at all -- e.g. a receiver who ran routes
and was never targeted -- is absent from every map and still voids, where a
book would grade his anytime-TD as a loss. That's rare for the high-usage
players these boards pick, and it's the conservative direction for a void
check to err. It is not zero, though.

ONE-TIME REPAIR: _repair_false_td_voids re-grades every TD-prop push using
the corrected check, once, then marks itself done in stats_cache. That fixes
the history in place, so no database upload is needed.

CLV: when a moneyline pick grades, compare the price we took to the CLOSING
line. Positive CLV means the market moved toward our side after we bet it.
"""

import json
import logging
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import requests

import config
from data.final_scores import get_final_score_espn
from data.td_settle import get_td_scorers
from data.player_settle import get_player_stats, grade_player_prop
from engine.player_props import MARKET_BY_LABEL

logger = logging.getLogger(__name__)

TD_VOID_REPAIR_MARKER = "repair:td_false_voids:v1"


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
    """Did this player log ANY line in the final box score?

    stats is {market_key: {normalized_name: value}} (data/player_settle).
    True  -> he appears under at least one market: he played
    False -> the box score is populated and he's in none of it: inactive
    None  -> no usable box score, so the caller must not guess"""
    if not stats or not isinstance(stats, dict):
        return None
    target = _norm_name(player_name)
    if not target:
        return None
    any_rows = False
    for player_map in stats.values():
        if not isinstance(player_map, dict):
            continue
        if player_map:
            any_rows = True
        if target in player_map:
            return True
    return False if any_rows else None


_PROP_RE = re.compile(r"^(?P<name>.+?)\s+(?P<side>Over|Under)\s+(?P<line>[\d.]+)\s+(?P<label>.+)$",
                      re.IGNORECASE)


def _parse_prop_label(label):
    """(name, market_key, side, line) or None. Handles the stored human format
    and the legacy pipe form."""
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


def _final_for(db, game_id, sport, rec_date, cache, unresolved=None):
    """(scores, date_found_under) or (None, None). Cached per game."""
    if game_id in cache:
        return cache[game_id]
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
        if result is None and unresolved is not None:
            unresolved.append(f"{sport} {game_id} ({row.get('away_team')}@{row.get('home_team')})")
    else:
        result = _get_final_score_mlb(game_id)
        used_date = rec_date
    cache[game_id] = (result, used_date)
    return result, used_date


def _repair_false_td_voids(db):
    """One-time: re-grade TD-prop pushes written by the broken participation
    check. Genuine voids stay pushes; wrongly-voided losses become losses."""
    try:
        with db.cursor() as cur:
            cur.execute("SELECT 1 FROM stats_cache WHERE key=?", (TD_VOID_REPAIR_MARKER,))
            if cur.fetchone():
                return 0
            cur.execute("SELECT * FROM recommendations WHERE kind='td_prop' AND status='push'")
            rows = [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        logger.warning("TD void repair skipped (couldn't read rows): %s", exc)
        return 0

    if not rows:
        _mark_repair_done(db, 0, 0)
        return 0

    final_cache, scorer_cache, box_cache = {}, {}, {}
    to_lost = to_won = kept = unknown = 0
    for rec in rows:
        gid = rec.get("game_id")
        if not gid:
            kept += 1
            continue
        scores, used_date = _final_for(db, gid, rec.get("sport") or "NFL",
                                       rec.get("date"), final_cache)
        if scores is None:
            unknown += 1
            continue
        row = db.get_game(gid) or {}
        day = used_date or rec.get("date")
        if gid not in scorer_cache:
            scorer_cache[gid] = get_td_scorers(day, row.get("home_team"), row.get("away_team"))
        if gid not in box_cache:
            box_cache[gid] = get_player_stats(day, row.get("home_team"), row.get("away_team"))
        scorers = scorer_cache[gid]
        if scorers is None:
            unknown += 1
            continue
        player = rec["side_or_player"]
        if _norm_name(player) in {_norm_name(n) for n in scorers}:
            db.set_recommendation_status(rec["id"], "won")
            to_won += 1
            continue
        played = _played_in_game(box_cache[gid], player)
        if played is True:
            db.set_recommendation_status(rec["id"], "lost")
            to_lost += 1
        elif played is False:
            kept += 1
        else:
            unknown += 1

    if unknown == 0:
        _mark_repair_done(db, to_lost, to_won)
    logger.warning("TD VOID REPAIR: re-graded %d wrongly-voided TD prop(s) -> %d lost, %d won; "
                   "%d genuine void(s) kept; %d unreadable%s.",
                   to_lost + to_won, to_lost, to_won, kept, unknown,
                   " (will retry next run)" if unknown else "")
    return to_lost + to_won


def _mark_repair_done(db, lost, won):
    try:
        with db.cursor() as cur:
            cur.execute("INSERT OR REPLACE INTO stats_cache (key, payload, cached_at) VALUES (?, ?, ?)",
                        (TD_VOID_REPAIR_MARKER, json.dumps({"lost": lost, "won": won}), time.time()))
    except Exception as exc:
        logger.debug("Couldn't write TD void repair marker: %s", exc)


def grade_pending(db):
    repaired = _repair_false_td_voids(db)

    pending = db.get_pending_recommendations()
    if not pending:
        return {"graded": 0, "td_graded": 0, "totals_graded": 0,
                "props_graded": 0, "voided": 0, "repaired": repaired}

    graded_count = td_graded = totals_graded = props_graded = voided = 0
    final_cache, td_cache, player_cache = {}, {}, {}
    by_date = {}
    unresolved = []

    def _box_score(game_id, used_date, rec_date):
        if game_id not in player_cache:
            row = db.get_game(game_id) or {}
            player_cache[game_id] = get_player_stats(
                used_date or rec_date, row.get("home_team"), row.get("away_team"))
        return player_cache[game_id]

    for rec in pending:
        sport = rec.get("sport") or "MLB"
        kind = rec["kind"]
        rec_date = rec.get("date")

        # ---- Anytime-TD props ----------------------------------------------
        if kind == "td_prop" and rec["game_id"]:
            scores, used_date = _final_for(db, rec["game_id"], sport, rec_date,
                                           final_cache, unresolved)
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
            if _norm_name(player) in {_norm_name(n) for n in scorers}:
                db.set_recommendation_status(rec["id"], "won")
                td_graded += 1
                logger.info("TD prop graded %s: %s -> won", rec_date, player)
                continue
            played = _played_in_game(_box_score(rec["game_id"], used_date, rec_date), player)
            if played is None:
                logger.info("TD prop %s (%s): box score unreadable -- leaving pending.",
                            rec_date, player)
                continue
            if played is False:
                db.set_recommendation_status(rec["id"], "push")
                voided += 1
                logger.info("TD prop VOIDED %s: %s logged no line in the final box score "
                            "(inactive).", rec_date, player)
                continue
            db.set_recommendation_status(rec["id"], "lost")
            td_graded += 1
            logger.info("TD prop graded %s: %s -> lost (played, did not score)", rec_date, player)
            continue

        # ---- Player props ---------------------------------------------------
        if kind == "player_prop" and rec["game_id"]:
            parsed = _parse_prop_label(rec["side_or_player"])
            if not parsed:
                logger.warning("Player prop %s has an unparseable label -- skipping: %s",
                               rec["id"], rec["side_or_player"])
                continue
            scores, used_date = _final_for(db, rec["game_id"], sport, rec_date,
                                           final_cache, unresolved)
            if scores is None:
                continue
            stats = _box_score(rec["game_id"], used_date, rec_date)
            if stats is None:
                continue
            name, market, side, line = parsed
            # Void a player who logged nothing at all, before a missing value
            # defaults to 0 and hands an inactive player's UNDER a free win.
            if _played_in_game(stats, name) is False:
                db.set_recommendation_status(rec["id"], "push")
                voided += 1
                logger.info("Player prop VOIDED %s: %s logged no line (inactive).", rec_date, name)
                continue
            status = grade_player_prop(stats, market, name, side, line)
            if status is None:
                continue
            db.set_recommendation_status(rec["id"], status)
            props_graded += 1
            logger.info("Player prop graded %s: %s %s %g (%s) -> %s",
                        rec_date, name, side, line, market, status)
            continue

        # ---- Totals ---------------------------------------------------------
        if kind == "total" and rec["game_id"]:
            scores, _ = _final_for(db, rec["game_id"], sport, rec_date, final_cache, unresolved)
            if scores is None:
                continue
            combined = scores[0] + scores[1]
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
        scores, _ = _final_for(db, rec["game_id"], sport, rec_date, final_cache, unresolved)
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

    logger.info("Grading pass complete: %d ML, %d TD, %d totals, %d player props settled%s.",
                graded_count, td_graded, totals_graded, props_graded,
                f", {voided} voided (player inactive)" if voided else "")
    if unresolved:
        logger.warning("Could NOT find a final score for %d game(s) on any candidate date "
                       "(they stay pending and will retry next run): %s",
                       len(unresolved), ", ".join(sorted(set(unresolved))[:10]))
    return {"graded": graded_count, "td_graded": td_graded,
            "totals_graded": totals_graded, "props_graded": props_graded,
            "voided": voided, "repaired": repaired}


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
