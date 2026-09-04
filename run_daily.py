#!/usr/bin/env python3
"""
run_daily.py -- the one command you run each day.

MLB IS MONEYLINE-ONLY as of Sep 3, 2026. HR props are retired (see config.py:
11-120, ROI -46% over 131 graded picks). Everything HR-related is stripped from
generation, history and records -- keeping the old losses in the ledger would
drag a discontinued bet type through every number the report shows.

NFL RUNS TWO SEPARATE PROP BOARDS (Sep 4, 2026):
  - anytime touchdown   (engine/td_props.py)      cap 10
  - yards / receptions / pass TDs (engine/player_props.py)  cap 10
They are ranked and capped INDEPENDENTLY and never merged. A touchdown prop
and a receiving-yards prop aren't comparable bets, so letting them compete for
the same ten slots would just mean whichever model happens to output bigger
numbers crowds the other off the page. Both caps are ceilings, not quotas: a
thin slate publishes four and that is the correct outcome.
"""

import argparse
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import auto_gate
import config
from data.db import Database
from data.schedule_provider import get_todays_games
from data.schedule_provider_wnba import get_todays_wnba_games
from data.schedule_provider_nfl import get_todays_nfl_games
from data.schedule_provider_ncaaf import get_todays_ncaaf_games
from data.schedule_provider_ncaab import get_todays_ncaab_games
from data.schedule_provider_nhl import get_todays_nhl_games
from data.schedule_provider_nba import get_todays_nba_games
from data.odds_api_schedule import schedule_from_odds_api
from data.odds_providers import get_odds_provider
from data.public_betting_provider import get_public_betting_provider
from data.stats_provider import get_stats_provider
from data.situational import park_and_situational_summary, ensure_injury_template
from data.standings import get_all_team_records
from data.standings_wnba import get_all_wnba_records
from data.standings_espn import get_all_records_for_sport
from data.standings_scores import get_records as get_scores_records
from data.lineups import get_confirmed_pitcher
from data.nfl_players import get_skill_players, get_td_profile, get_player_profile
from data.td_odds import fetch_td_odds
from data.prop_odds import fetch_player_prop_odds
import re as _re
import unicodedata as _ud


def _norm_player(name):
    if not name:
        return ""
    n = _ud.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    n = _re.sub(r"[.\,']", "", n)
    n = _re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", n)
    return _re.sub(r"\s+", " ", n).strip()
from data.celestial import celestial_signal_for, moon_phase_for, moon_sign_for
from data.numerology import numerology_signal_for, reduce_date

from engine.scoring import evaluate_game
from engine.strategy_rules import select_daily_plays, select_fade_teams, get_parlay_pool
from engine.td_props import evaluate_td_candidates, finalize_td_props
from engine.player_props import (evaluate_player_props, finalize_player_props,
                                  label_for as player_prop_label)
from engine.totals import evaluate_totals, label_for as total_label
from engine.parlay import maybe_build_parlay, build_daily_parlay, build_double_parlay
from engine.models import DailyReport, ProbablePitcher, MoneylineOdds

from output.terminal_report import print_daily_report
from output.html_report import render_daily_report
from output.history_log import log_recommendations, bankroll_summary, get_pick_changes
from output.publish_github_pages import publish_latest_report
from output.publish_whop import publish_to_whop

from backtest.grader import grade_pending

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, logging.INFO),
                     format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("run_daily")

LEDGER_CUTOFF = "2026-07-26"

SEED_OVERRIDE_DATES = {"2026-07-29", "2026-07-30"}

SPORT_ORDER = ["MLB", "WNBA", "NFL", "NCAAF", "NCAAB", "NHL", "NBA"]

RECORD_SPORTS = {"MLB", "WNBA", "NFL", "NCAAF", "NCAAB", "NHL", "NBA"}
SCORES_RECORD_SPORTS = {"WNBA", "NFL", "NCAAF", "NCAAB", "NHL", "NBA"}

# Bet kinds that no longer belong anywhere in the report or the records.
RETIRED_KINDS = {"hr_prop"}

_ESPN_SCHEDULE_PROVIDERS = {
    "NFL": get_todays_nfl_games,
    "NCAAF": get_todays_ncaaf_games,
    "NCAAB": get_todays_ncaab_games,
    "NHL": get_todays_nhl_games,
    "NBA": get_todays_nba_games,
}

TD_ROTATION_LOOKBACK_DAYS = 14
TD_HARD_BENCH_DAYS = 7


def _fetch_schedule(sport, date_str):
    if sport == "MLB":
        return get_todays_games(date_str)
    if sport == "WNBA":
        return get_todays_wnba_games(date_str)
    provider = _ESPN_SCHEDULE_PROVIDERS.get(sport)
    if provider:
        games = provider(date_str)
        if games:
            return games
        return schedule_from_odds_api(sport, date_str)
    logger.warning("No schedule provider wired for enabled sport %s -- skipping.", sport)
    return []


def _active_sports_today(run_date):
    """Only query leagues that can actually be playing, so out-of-season sports
    don't burn Odds API credits returning nothing."""
    live = [s for s in config.ENABLED_SPORTS if config.in_season(s, run_date)]
    skipped = [s for s in config.ENABLED_SPORTS if s not in live]
    if skipped:
        logger.info("Season gate: querying %s | skipping out-of-season %s (saves API credits).",
                    ", ".join(live), ", ".join(skipped))
    return live


def _recent_prop_players(db, run_date, kind, lookback_days, bench_days, max_appearances=None):
    """Normalized player names to fade for rotation, so the same handful of
    names doesn't repeat on the board week after week."""
    appearances = {}
    recent = set()
    for i in range(1, lookback_days + 1):
        d = (run_date - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            rows = db.get_recommendations_for_date(d, kind=kind)
        except Exception as exc:
            logger.warning("%s rotation: could not read %s: %s", kind, d, exc)
            continue
        seen_today = set()
        for r in rows:
            key = _norm_player(r["side_or_player"])
            if not key or key in seen_today:
                continue
            seen_today.add(key)
            appearances[key] = appearances.get(key, 0) + 1
            if i <= bench_days:
                recent.add(key)
    cold = set(recent)
    if max_appearances:
        cold |= {k for k, n in appearances.items() if n >= max_appearances}
    if cold:
        logger.info("%s rotation fade (%d): %s", kind, len(cold), ", ".join(sorted(cold)))
    return cold


def _locked_props(db, date_str, pool, kind, name_key="player_name", match_fn=None):
    """If today's props of this kind are already logged, reuse those exact
    picks (with fresh odds/reasoning) so the board never shifts mid-day.

    match_fn lets a caller key on something other than the player's name --
    player props need the full 'Player Over 62.5 Receiving Yards' label,
    because the same man can legitimately appear under two different markets
    and matching on name alone would collapse them into one."""
    try:
        existing = db.get_recommendations_for_date(date_str, kind=kind)
    except Exception as exc:
        logger.warning("%s lock: could not read today's rows: %s", kind, exc)
        return None
    keys, seen = [], set()
    for r in existing:
        k = _norm_player(r["side_or_player"])
        if k and k not in seen:
            seen.add(k)
            keys.append(k)
    if not keys:
        return None
    by_key = {}
    for c in pool:
        k = _norm_player(match_fn(c) if match_fn else c[name_key])
        by_key.setdefault(k, c)
    locked = [by_key[k] for k in keys if k in by_key]
    for c in locked:
        c["pick_type"] = "core"
    if locked:
        logger.info("%s LOCKED to today's published picks (%d).", kind, len(locked))
        return locked
    return None


def _load_team_records(games, run_date, data_warnings):
    records = get_all_team_records()  # MLB
    for sport in SCORES_RECORD_SPORTS:
        if not any(g.sport == sport for g in games):
            continue
        sp = get_scores_records(sport, season=run_date.year)
        if not sp:
            sp = (get_all_wnba_records(season=run_date.year) if sport == "WNBA"
                  else get_all_records_for_sport(sport, season=run_date.year))
        if sp:
            records.update(sp)
        else:
            data_warnings.append(
                f"{sport} records still building (no results stored yet) -- {sport} talent/motivation "
                f"factors run neutral until a few games are logged.")
    return records


def _row_to_odds(row):
    return MoneylineOdds(
        book=row["book"], home_ml=row["home_ml"], away_ml=row["away_ml"],
        captured_at=row["captured_at"], home_spread=row["home_spread"],
        away_spread=row["away_spread"], total=row["total"],
    )


def _nfl_rosters_and_profiles(nfl_games):
    """Skill rosters + full stat profiles for both prop boards.

    Fetched ONCE and shared: the TD board and the yardage board need the same
    players and the same underlying stats, and pulling them twice would double
    the ESPN calls for no benefit."""
    rosters_by_team = {}
    profiles = {}
    for game in nfl_games:
        for team in (game.home_team, game.away_team):
            if team in rosters_by_team:
                continue
            players = get_skill_players(team)
            rosters_by_team[team] = players
            for p in players:
                pid = p["player_id"]
                if pid not in profiles:
                    profiles[pid] = get_player_profile(pid, p["name"])
    return rosters_by_team, profiles


def _build_td_props(db, nfl_games, rosters_by_team, profiles, odds_by_game,
                    date_str, run_date, data_warnings):
    """Board 1: NFL anytime-TD props, Poisson-modelled, locked per day."""
    if not nfl_games:
        return []

    # td_props.py wants the narrower TD view of each profile.
    td_profiles = {}
    for pid, prof in profiles.items():
        if not prof:
            continue
        td_profiles[pid] = {
            "season": prof["season"], "games": prof["games"],
            "rush_td": int(prof["rush_td"]), "rec_td": int(prof["rec_td"]),
            "total_td": prof["total_td"], "td_per_game": prof["td_per_game"],
            "touches": prof["touches"], "targets": int(prof["targets"]),
        }

    if not td_profiles:
        data_warnings.append(
            "NFL TD props: no player TD history loaded (ESPN athlete stats unavailable) -- "
            "TD props are skipped today.")
        return []

    pool = evaluate_td_candidates(nfl_games, rosters_by_team, td_profiles, odds_by_game)
    if not pool:
        return []

    prices = fetch_td_odds(pool, nfl_games)
    for c in pool:
        price = prices.get((c["game_id"], _norm_player(c["player_name"])))
        c["odds_american"] = price["odds"] if price else None
        c["odds_book"] = price["book"] if price else None

    cold = _recent_prop_players(db, run_date, "td_prop",
                                TD_ROTATION_LOOKBACK_DAYS, TD_HARD_BENCH_DAYS)
    fresh_board = finalize_td_props(pool, recent_players=cold)
    locked = _locked_props(db, date_str, pool, "td_prop")
    board = locked if locked is not None else fresh_board

    if board and all(c.get("odds_american") is None for c in board):
        data_warnings.append(
            "NFL TD props are showing without prices -- anytime-TD is a paid player-props "
            "market on The Odds API. The picks are still model-ranked; confirm the price yourself.")
    return board


def _build_player_props(db, nfl_games, rosters_by_team, profiles, odds_by_game,
                        date_str, data_warnings):
    """Board 2: NFL yards / receptions / pass TDs.

    Unlike the TD board there is NO rotation fade here. Rotation exists to stop
    the same three names recurring on a 3-slot board; with ten slots across
    five markets the board naturally turns over, and benching a player whose
    line is genuinely soft would mean passing on the edge we're paid to find."""
    if not nfl_games or not getattr(config, "PLAYER_PROPS_ENABLED", False):
        return []
    if not profiles:
        return []

    prop_odds = fetch_player_prop_odds(nfl_games)
    if not prop_odds:
        logger.info("Player props: no posted lines returned -- board is empty today.")
        return []

    injuries = _injury_status_map(nfl_games)
    pool = evaluate_player_props(nfl_games, rosters_by_team, profiles, prop_odds,
                                 odds_by_game, injuries_by_player=injuries)
    if not pool:
        return []

    fresh_board = finalize_player_props(pool)
    locked = _locked_props(db, date_str, pool, "player_prop",
                           match_fn=player_prop_label)
    return locked if locked is not None else fresh_board


def _injury_status_map(nfl_games):
    """{player_name: status} from ESPN's per-game injury block, used to skip
    UNDERS on questionable players -- a scratch voids the bet at most books but
    grades UNDER at a few, so the bet's own settlement rules are unreliable."""
    from data.espn_fetch import fetch_scoreboard_events
    out = {}
    dates = {g.date for g in nfl_games}
    for date_str in dates:
        try:
            events = fetch_scoreboard_events("football/nfl", date_str,
                                             season_types=(None, 1, 2, 3))
        except Exception as exc:
            logger.debug("Injury fetch failed for %s: %s", date_str, exc)
            continue
        for ev in events or []:
            for comp in ev.get("competitions", []):
                for inj in comp.get("injuries", []) or []:
                    athlete = (inj.get("athlete") or {}).get("displayName")
                    status = inj.get("status") or (inj.get("type") or {}).get("description")
                    if athlete and status:
                        out[athlete] = str(status)
    if out:
        logger.info("Injury map: %d NFL player designation(s) loaded.", len(out))
    return out


def _log_props(db, date_str, rows, kind, sport, name_key, label_fn=None):
    """Store props/totals so they lock for the day and reach history."""
    if not rows:
        return
    try:
        if db.get_recommendations_for_date(date_str, kind=kind):
            return
    except Exception:
        pass
    now_iso = datetime.now(timezone.utc).isoformat()
    for c in rows:
        try:
            db.insert_recommendation(
                date=date_str, game_id=c.get("game_id"), kind=kind,
                side_or_player=(label_fn(c) if label_fn else c[name_key]),
                team=c.get("team"), sport=c.get("sport") or sport,
                odds_american=c.get("odds_american"),
                edge_pct=c.get("ev_edge", c.get("edge_pct")),
                model_prob=c.get("model_prob"), market_prob=c.get("market_prob", c.get("implied_prob")),
                stake_units=1.0, stake_dollars=0.0,
                reasoning=c.get("reasoning", []), factor_scores=[],
                created_at=now_iso,
            )
        except Exception as exc:
            logger.warning("Could not log %s %s: %s", kind, c.get(name_key), exc)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run today's betting recommendation pipeline.")
    parser.add_argument("--date", default=None, help="Run as if it were this date (YYYY-MM-DD).")
    parser.add_argument("--skip-grading", action="store_true", help="Skip grading yesterday's picks first.")
    parser.add_argument("--auto", action="store_true",
                         help="Scheduled mode: only publish once, ~1 hour before the first game.")
    args = parser.parse_args(argv)

    run_date = (datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
                else datetime.now(ZoneInfo(config.TIMEZONE)).date())
    date_str = run_date.strftime("%Y-%m-%d")

    live_sports = _active_sports_today(run_date)

    games = []
    for sport in live_sports:
        games.extend(_fetch_schedule(sport, date_str))

    if args.auto:
        should_run, reason = auto_gate.should_run_now(run_date, date_str, games)
        logger.info("Auto-run check: %s", reason)
        if not should_run:
            return

    db = Database()

    if not args.skip_grading:
        result = grade_pending(db)
        for key, label in (("graded", "moneyline pick(s)"), ("td_graded", "TD prop(s)"),
                           ("player_prop_graded", "player prop(s)"), ("totals_graded", "total(s)")):
            if result.get(key):
                logger.info("Graded %s %s from prior days.", result[key], label)

    data_warnings = []

    if not games:
        logger.info("No games found across in-season sports (%s) for %s.", ", ".join(live_sports), date_str)
        history = _build_history(db, date_str)
        report = DailyReport(date=date_str, slate_size=0, plays=[], fade_teams=[], hr_props=[], parlay=None,
                              dropped_notes=[], celestial=_celestial_dict(run_date),
                              numerology=_numerology_dict(run_date),
                              bankroll_summary=bankroll_summary(db, history),
                              data_warnings=["No games on today's schedule across in-season sports."],
                              results_recap=_build_results_recap(db, date_str),
                              history=history,
                              sport_parlays={}, top_parlay={}, double_parlay={}, active_sports=[],
                              pick_changes=get_pick_changes(date_str),
                              td_props=[], player_props=[], totals=[])
        _emit(report)
        if args.auto:
            auto_gate.mark_published(date_str)
        return

    if len(games) < config.MIN_SLATE_SIZE:
        data_warnings.append(f"Small slate today ({len(games)} games) -- confidence in every edge is lower.")

    for game in games:
        db.upsert_game(game)

    missing_pitchers = [g for g in games if g.sport == "MLB" and (not g.home_pitcher or not g.away_pitcher)]
    if missing_pitchers:
        data_warnings.append(
            f"{len(missing_pitchers)} MLB game(s) have no probable pitcher posted by MLB yet -- "
            f"pitching-matchup grading is skipped for those until confirmed: "
            + ", ".join(f"{g.away_team}@{g.home_team}" for g in missing_pitchers[:6])
            + (f" +{len(missing_pitchers) - 6} more" if len(missing_pitchers) > 6 else "")
        )

    odds_by_game = {}
    for sport in live_sports:
        sport_games = [g for g in games if g.sport == sport]
        if not sport_games:
            continue
        odds_by_game.update(get_odds_provider(sport).get_odds(sport_games))

    unpriced = [g for g in games if g.game_id not in odds_by_game]
    if unpriced:
        data_warnings.append(
            f"{len(unpriced)} game(s) had no real market price and were left out of the slate "
            f"entirely. The engine never invents a price -- fewer picks is the correct outcome.")

    now_iso = datetime.now(timezone.utc).isoformat()
    for game in games:
        odds = odds_by_game.get(game.game_id)
        if not odds or odds.book == "mock":
            continue
        is_opening = db.get_opening_line(game.game_id) is None
        db.record_odds_snapshot(game.game_id, odds, now_iso, is_opening=is_opening)

    ensure_injury_template(date_str)
    public_splits = get_public_betting_provider().get_splits(games, date_str)
    for game in games:
        split = public_splits.get(game.game_id)
        if not split:
            continue
        db.record_public_split(game.game_id, split, now_iso)
        if split.data_quality in ("missing", "mock"):
            data_warnings.append(
                f"{game.away_team} @ {game.home_team}: public betting % is "
                f"{'simulated' if split.data_quality == 'mock' else 'not yet filled in'} -- "
                f"edit manual_inputs/public_betting_{date_str}.json for a sharper read."
            )

    stats_provider = get_stats_provider()
    team_records = _load_team_records(games, run_date, data_warnings)
    if not team_records:
        data_warnings.append("Standings unavailable today -- talent gap & motivation factors are running blind.")

    # Confirmed MLB starters override MLB's listed probables, which go stale.
    for game in games:
        if game.sport != "MLB":
            continue
        home_conf = get_confirmed_pitcher(game.game_id, "home")
        away_conf = get_confirmed_pitcher(game.game_id, "away")
        if home_conf and game.home_pitcher and home_conf != game.home_pitcher.name:
            logger.info("Confirmed home starter %s overrides stale probable %s (game %s)",
                        home_conf, game.home_pitcher.name, game.game_id)
        if home_conf:
            game.home_pitcher = ProbablePitcher(name=home_conf, player_id=None)
        if away_conf:
            game.away_pitcher = ProbablePitcher(name=away_conf, player_id=None)
        game.pitchers_confirmed = bool(home_conf and away_conf)

    evaluations = []
    for game in games:
        odds = odds_by_game.get(game.game_id)
        if not odds:
            continue
        is_mlb = game.sport == "MLB"
        has_records = game.sport in RECORD_SPORTS
        home_pitcher_profile = (stats_provider.get_pitcher_profile(game.home_pitcher.name, game.home_pitcher.player_id)
                                 if is_mlb and game.home_pitcher else None)
        away_pitcher_profile = (stats_provider.get_pitcher_profile(game.away_pitcher.name, game.away_pitcher.player_id)
                                 if is_mlb and game.away_pitcher else None)
        home_offense = stats_provider.get_team_offense_profile(game.home_team) if is_mlb else None
        away_offense = stats_provider.get_team_offense_profile(game.away_team) if is_mlb else None
        situational = (park_and_situational_summary(game.home_team, game.away_team, date_str)
                       if is_mlb else {})
        ev = evaluate_game(
            game, odds, home_pitcher_profile, away_pitcher_profile, home_offense, away_offense,
            team_records.get(game.home_team, {}) if has_records else {},
            team_records.get(game.away_team, {}) if has_records else {},
            public_splits.get(game.game_id), situational, run_date=run_date,
        )
        evaluations.append(ev)

    plays, dropped_notes = select_daily_plays(evaluations, db, public_splits, date_str)
    fade_teams = select_fade_teams(evaluations)

    # ---- the two NFL prop boards, built off one shared roster/stat pull ----
    nfl_games = [g for g in games
                 if g.sport == "NFL" and not getattr(g, "is_preseason", False)]
    td_props = []
    player_props = []
    if nfl_games:
        rosters_by_team, profiles = _nfl_rosters_and_profiles(nfl_games)
        td_props = _build_td_props(db, nfl_games, rosters_by_team, profiles,
                                   odds_by_game, date_str, run_date, data_warnings)
        player_props = _build_player_props(db, nfl_games, rosters_by_team, profiles,
                                           odds_by_game, date_str, data_warnings)

    totals = evaluate_totals(games, odds_by_game, team_records)

    raw_celestial, _, _ = celestial_signal_for(run_date)
    raw_numerology, _, _ = numerology_signal_for(run_date)
    parlay_pool = get_parlay_pool(evaluations)
    parlay = maybe_build_parlay(parlay_pool, raw_celestial, raw_numerology)

    sport_parlays = {}
    active_sports = [s for s in SPORT_ORDER if any(g.sport == s for g in games)]
    for sport in active_sports:
        sp_plays = [p for p in plays if p.sport == sport]
        par = build_daily_parlay(sp_plays, [])
        if par:
            sport_parlays[sport] = par
    top_parlay = build_daily_parlay(plays, [])
    double_parlay = build_double_parlay(plays)

    first_starts = [g.game_time_utc for g in games if g.game_time_utc]
    earliest_start = min(first_starts) if first_starts else None

    log_recommendations(db, date_str, plays, [], top_parlay,
                        sport_parlays=sport_parlays, double_parlay=double_parlay,
                        first_pitch_utc=earliest_start)
    _log_props(db, date_str, td_props, "td_prop", "NFL", "player_name")
    _log_props(db, date_str, player_props, "player_prop", "NFL", "player_name",
               label_fn=player_prop_label)
    _log_props(db, date_str, totals, "total", "NCAAF", "matchup", label_fn=total_label)

    history = _build_history(db, date_str)
    report = DailyReport(
        date=date_str, slate_size=len(games), plays=plays, fade_teams=fade_teams, hr_props=[], parlay=parlay,
        dropped_notes=dropped_notes, celestial=_celestial_dict(run_date),
        numerology=_numerology_dict(run_date), bankroll_summary=bankroll_summary(db, history),
        data_warnings=data_warnings, results_recap=_build_results_recap(db, date_str),
        history=history,
        daily_parlay=top_parlay,
        sport_parlays=sport_parlays, top_parlay=top_parlay, double_parlay=double_parlay,
        active_sports=active_sports,
        pick_changes=get_pick_changes(date_str),
        td_props=td_props, player_props=player_props, totals=totals,
    )
    _emit(report)
    if args.auto:
        auto_gate.mark_published(date_str)


def _label_for(row):
    """History label for a stored recommendation, or None if the kind is
    retired or not something we display."""
    kind = row["kind"]
    if kind in RETIRED_KINDS:
        return None
    odds = row["odds_american"]
    if kind == "moneyline":
        return f"{row['team']} ML ({odds:+d})" if odds is not None else f"{row['team']} ML"
    if kind == "td_prop":
        return (f"{row['side_or_player']} anytime TD ({odds:+d})" if odds is not None
                else f"{row['side_or_player']} anytime TD")
    if kind == "player_prop":
        # Already stored as a full sentence: "Player Over 62.5 Receiving Yards".
        return (f"{row['side_or_player']} ({odds:+d})" if odds is not None
                else row["side_or_player"])
    if kind == "total":
        return row["side_or_player"]
    return None


def _build_history(db, today_str):
    """Past graded slates, newest first -- STRICTLY days before today_str.

    HR props are filtered out entirely (RETIRED_KINDS). They were 11-120, and
    leaving them in would keep a discontinued bet type dragging down every
    record on the page. The seed days below are moneyline-only for the same
    reason."""
    seed = [
        {"date": "2026-07-30",
         "parlay": ["PIT ML (-112)", "TB ML (-178)", "ATL ML (-154)", "CWS ML (-116)"],
         "double": [],
         "picks": [
            {"label": "TB ML (-178)", "status": "won", "kind": "moneyline", "sport": "MLB"},
            {"label": "ATL ML (-154)", "status": "won", "kind": "moneyline", "sport": "MLB"},
            {"label": "CWS ML (-116)", "status": "won", "kind": "moneyline", "sport": "MLB"},
            {"label": "PIT ML (-112)", "status": "lost", "kind": "moneyline", "sport": "MLB"},
            {"label": "MIA ML (+110)", "status": "lost", "kind": "moneyline", "sport": "MLB"},
        ]},
        {"date": "2026-07-29",
         "parlay": ["TOR ML", "TB ML", "ATL (Gm 2) ML", "BOS ML"],
         "double": [],
         "picks": [
            {"label": "TOR ML", "status": "won", "kind": "moneyline", "sport": "MLB"},
            {"label": "TB ML", "status": "won", "kind": "moneyline", "sport": "MLB"},
            {"label": "ATL (Gm 2) ML", "status": "won", "kind": "moneyline", "sport": "MLB"},
            {"label": "BOS ML", "status": "won", "kind": "moneyline", "sport": "MLB"},
        ]},
        {"date": "2026-07-26",
         "parlay": ["BOS ML (-112)", "ARI ML (+102)", "MIL ML (-238)", "CWS ML (+109)"],
         "double": [],
         "picks": [
            {"label": "BOS ML (-112)", "status": "won", "kind": "moneyline", "sport": "MLB"},
            {"label": "MIL ML (-238)", "status": "won", "kind": "moneyline", "sport": "MLB"},
            {"label": "CWS ML (+109)", "status": "won", "kind": "moneyline", "sport": "MLB"},
            {"label": "MIN ML", "status": "won", "kind": "moneyline", "sport": "MLB"},
            {"label": "ARI ML (+102)", "status": "lost", "kind": "moneyline", "sport": "MLB"},
        ]},
        {"date": "2026-07-25",
         "parlay": ["TB ML (-120)", "STL ML (-112)", "WSH ML (-134)", "ARI ML"],
         "double": [],
         "picks": [
            {"label": "ARI ML", "status": "won", "kind": "moneyline", "sport": "MLB"},
            {"label": "WSH ML (-134)", "status": "won", "kind": "moneyline", "sport": "MLB"},
            {"label": "STL ML (-112)", "status": "won", "kind": "moneyline", "sport": "MLB"},
            {"label": "TB ML (-120)", "status": "won", "kind": "moneyline", "sport": "MLB"},
            {"label": "MIA ML (-142)", "status": "lost", "kind": "moneyline", "sport": "MLB"},
        ]},
        {"date": "2026-07-24",
         "parlay": [],
         "double": [],
         "picks": [
            {"label": "ARI ML (-124)", "status": "won", "kind": "moneyline", "sport": "MLB"},
            {"label": "MIL ML (-122)", "status": "lost", "kind": "moneyline", "sport": "MLB"},
            {"label": "MIN ML (-144)", "status": "lost", "kind": "moneyline", "sport": "MLB"},
        ]},
    ]
    by_date = {}
    order = []
    for r in db.get_graded_history(after=LEDGER_CUTOFF):
        d = r["date"]
        if d >= today_str or d in SEED_OVERRIDE_DATES:
            continue
        label = _label_for(r)
        if label is None:
            continue
        if d not in by_date:
            by_date[d] = []
            order.append(d)
        by_date[d].append({"label": label, "status": r["status"], "kind": r["kind"],
                            "sport": r.get("sport") or "MLB"})
    db_days = []
    for d in order:
        parlay_rows = db.get_recommendations_for_date(d, kind="parlay_leg")
        double_rows = db.get_recommendations_for_date(d, kind="double_parlay_leg")
        db_days.append({
            "date": d,
            "picks": by_date[d],
            "parlay": [r["side_or_player"] for r in parlay_rows],
            "double": [r["side_or_player"] for r in double_rows],
        })
    all_days = db_days + seed
    all_days.sort(key=lambda x: x["date"], reverse=True)
    return [d for d in all_days if d["date"] < today_str]


def _build_results_recap(db, date_str):
    recap_date = db.get_last_slate_date(date_str)
    if not recap_date:
        return {}
    items = []
    for r in db.get_recommendations_for_date(recap_date):
        if r["status"] not in ("won", "lost", "push"):
            continue
        label = _label_for(r)
        if label is None:
            continue
        items.append({"label": label, "status": r["status"], "kind": r["kind"],
                      "sport": r.get("sport") or "MLB"})
    return {"date": recap_date, "picks": items}


def _celestial_dict(run_date):
    phase, illum = moon_phase_for(run_date)
    return {"phase": phase, "illumination": illum, "sign": moon_sign_for(run_date)}


def _numerology_dict(run_date):
    return {"number": reduce_date(run_date)}


def _emit(report):
    print_daily_report(report)
    path, html = render_daily_report(report)
    logger.info("HTML report written to %s", path)

    publish_result = publish_latest_report(html)
    if publish_result.get("published"):
        logger.info("Live at %s", publish_result["url"])

    # No-op unless WHOP_API_KEY / WHOP_EXPERIENCE_ID are set. They're
    # deliberately unset -- posting to the community is manual.
    whop_result = publish_to_whop(report)
    if whop_result.get("published"):
        logger.info("Posted to Whop forum (post %s).", whop_result.get("post_id"))


if __name__ == "__main__":
    main()
