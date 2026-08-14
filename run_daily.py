#!/usr/bin/env python3
"""
run_daily.py -- the one command you run each day.
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
from data.situational import park_and_situational_summary, ensure_injury_template, team_situational_summary
from data.standings import get_all_team_records
from data.standings_wnba import get_all_wnba_records
from data.standings_espn import get_all_records_for_sport
from data.standings_scores import get_records as get_scores_records
from data.rosters import get_team_batters
from data.lineups import get_confirmed_lineup, get_confirmed_pitcher
from data.hr_odds import fetch_hr_odds
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
from engine.hr_props import evaluate_hr_prop_candidates, finalize_hr_props
from engine.parlay import maybe_build_parlay, build_daily_parlay, build_double_parlay
from engine.models import DailyReport, ProbablePitcher, MoneylineOdds

from output.terminal_report import print_daily_report
from output.html_report import render_daily_report
from output.history_log import log_recommendations, bankroll_summary, get_pick_changes
from output.publish_github_pages import publish_latest_report

from backtest.grader import grade_pending

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, logging.INFO),
                     format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("run_daily")

LEDGER_CUTOFF = "2026-07-26"

SEED_OVERRIDE_DATES = {"2026-07-29", "2026-07-30"}

SPORT_ORDER = ["MLB", "WNBA", "NFL", "NCAAF", "NCAAB", "NHL", "NBA"]

RECORD_SPORTS = {"MLB", "WNBA", "NFL", "NCAAF", "NCAAB", "NHL", "NBA"}
SCORES_RECORD_SPORTS = {"WNBA", "NFL", "NCAAF", "NCAAB", "NHL", "NBA"}

_ESPN_SCHEDULE_PROVIDERS = {
    "NFL": get_todays_nfl_games,
    "NCAAF": get_todays_ncaaf_games,
    "NCAAB": get_todays_ncaab_games,
    "NHL": get_todays_nhl_games,
    "NBA": get_todays_nba_games,
}

# HR anti-repeat (rotation). HARD rule, no win-exemption: a batter is faded from
# today's board if he appeared on the HR board at all in the last
# HR_HARD_BENCH_DAYS days, OR was picked HR_ROTATION_MAX_APPEARANCES+ times in
# the last HR_ROTATION_LOOKBACK_DAYS. A single homer no longer buys permanent
# eligibility; everyone rotates.
HR_ROTATION_LOOKBACK_DAYS = 7
HR_ROTATION_MAX_APPEARANCES = 2
HR_HARD_BENCH_DAYS = 3


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


def _recent_hr_missers(db, run_date):
    """NORMALIZED names to fade off today's HR board for ROTATION. HARD rule --
    NO win-exemption. Counts every day a player was PICKED in the last
    HR_ROTATION_LOOKBACK_DAYS (deduped per day). A player is faded if he
    appeared on any of the last HR_HARD_BENCH_DAYS days, OR was picked
    HR_ROTATION_MAX_APPEARANCES+ times in the window. Homering does not exempt
    him -- everyone rotates so fresh names surface."""
    appearances = {}
    recent_days = set()
    for i in range(1, HR_ROTATION_LOOKBACK_DAYS + 1):
        d = (run_date - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            rows = db.get_recommendations_for_date(d, kind="hr_prop")
        except Exception as exc:
            logger.warning("HR rotation: could not read %s: %s", d, exc)
            continue
        seen_today = set()
        for r in rows:
            key = _norm_player(r["side_or_player"])
            if not key or key in seen_today:
                continue
            seen_today.add(key)
            appearances[key] = appearances.get(key, 0) + 1
            if i <= HR_HARD_BENCH_DAYS:
                recent_days.add(key)

    cold = {key for key, n in appearances.items()
            if n >= HR_ROTATION_MAX_APPEARANCES or key in recent_days}
    if cold:
        logger.info("HR-DIAG: rotation fade (%d bats, hard no-repeat over %dd): %s",
                    len(cold), HR_ROTATION_LOOKBACK_DAYS, ", ".join(sorted(cold)))
    return cold


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


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run today's betting recommendation pipeline.")
    parser.add_argument("--date", default=None, help="Run as if it were this date (YYYY-MM-DD).")
    parser.add_argument("--skip-grading", action="store_true", help="Skip grading yesterday's picks first.")
    parser.add_argument("--auto", action="store_true",
                         help="Scheduled mode: only publish once, ~1 hour before first pitch.")
    args = parser.parse_args(argv)

    run_date = (datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
                else datetime.now(ZoneInfo(config.TIMEZONE)).date())
    date_str = run_date.strftime("%Y-%m-%d")

    games = []
    for sport in config.ENABLED_SPORTS:
        games.extend(_fetch_schedule(sport, date_str))

    if args.auto:
        should_run, reason = auto_gate.should_run_now(run_date, date_str, games)
        logger.info("Auto-run check: %s", reason)
        if not should_run:
            return

    db = Database()

    if not args.skip_grading:
        result = grade_pending(db)
        if result.get("graded"):
            logger.info("Graded %s moneyline pick(s) from prior days.", result["graded"])
        if result.get("hr_graded"):
            logger.info("Graded %s HR prop(s) from prior days.", result["hr_graded"])

    data_warnings = []

    if not games:
        logger.info("No games found across enabled sports (%s) for %s.", ", ".join(config.ENABLED_SPORTS), date_str)
        history = _build_history(db, date_str)
        report = DailyReport(date=date_str, slate_size=0, plays=[], fade_teams=[], hr_props=[], parlay=None,
                              dropped_notes=[], celestial=_celestial_dict(run_date),
                              numerology=_numerology_dict(run_date),
                              bankroll_summary=bankroll_summary(db, history),
                              data_warnings=["No games on today's schedule across enabled sports."],
                              results_recap=_build_results_recap(db, date_str),
                              history=history,
                              sport_parlays={}, top_parlay={}, double_parlay={}, active_sports=[],
                              pick_changes=get_pick_changes(date_str))
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
            f"pitching-matchup grading and HR props are skipped for those until confirmed: "
            + ", ".join(f"{g.away_team}@{g.home_team}" for g in missing_pitchers[:6])
            + (f" +{len(missing_pitchers) - 6} more" if len(missing_pitchers) > 6 else "")
        )

    odds_by_game = {}
    for sport in config.ENABLED_SPORTS:
        sport_games = [g for g in games if g.sport == sport]
        if not sport_games:
            continue
        odds_by_game.update(get_odds_provider(sport).get_odds(sport_games))

    now_iso = datetime.now(timezone.utc).isoformat()
    for game in games:
        odds = odds_by_game.get(game.game_id)
        if not odds:
            continue
        if odds.book == "mock":
            real = db.get_last_real_line(game.game_id)
            if real:
                odds = _row_to_odds(real)
                odds_by_game[game.game_id] = odds
                logger.info("Game %s already started/not in live feed -- restored last real FanDuel line.", game.game_id)
        if odds.book != "mock":
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

    rosters = {}
    situational_by_team = {}
    lineup_source = {}
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

        for team, side in ((game.home_team, "home"), (game.away_team, "away")):
            if team in rosters:
                continue
            confirmed = get_confirmed_lineup(game.game_id, side)
            if confirmed:
                rosters[team] = confirmed
                lineup_source[team] = "confirmed"
            else:
                rosters[team] = get_team_batters(team)
                lineup_source[team] = "roster"
            situational_by_team[team] = team_situational_summary(team, date_str)

    if lineup_source and all(v == "roster" for v in lineup_source.values()):
        data_warnings.append(
            "Starting lineups haven't posted yet -- HR picks are drawn from active rosters "
            "(may include players who end up benched). Re-run closer to first pitch for confirmed lineups."
        )

    hr_props = []
    if config.HR_PROPS_ENABLED:
        mlb_games = [g for g in games if g.sport == "MLB"]
        hr_pool = evaluate_hr_prop_candidates(mlb_games, rosters, stats_provider, {},
                                              situational_by_team, lineup_source)
        hr_odds = fetch_hr_odds(hr_pool, games)
        for prop in hr_pool:
            key = (prop.get("game_id"), _norm_player(prop["player_name"]))
            price = hr_odds.get(key)
            if price:
                prop["odds_american"] = price["odds"]
                prop["odds_book"] = price["book"]
            else:
                prop["odds_american"] = None
                prop["odds_book"] = None
        recent_missers = _recent_hr_missers(db, run_date)
        hr_props = finalize_hr_props(hr_pool, recent_miss_players=recent_missers)

    raw_celestial, _, _ = celestial_signal_for(run_date)
    raw_numerology, _, _ = numerology_signal_for(run_date)
    parlay_pool = get_parlay_pool(evaluations)
    parlay = maybe_build_parlay(parlay_pool, raw_celestial, raw_numerology)

    sport_parlays = {}
    active_sports = [s for s in SPORT_ORDER if any(g.sport == s for g in games)]
    for sport in active_sports:
        sp_plays = [p for p in plays if p.sport == sport]
        sp_hr = hr_props if sport == "MLB" else []
        par = build_daily_parlay(sp_plays, sp_hr)
        if par:
            sport_parlays[sport] = par
    top_parlay = build_daily_parlay(plays, hr_props)
    double_parlay = build_double_parlay(plays)

    first_pitches = [g.game_time_utc for g in games if g.game_time_utc]
    earliest_first_pitch = min(first_pitches) if first_pitches else None

    log_recommendations(db, date_str, plays, hr_props, top_parlay,
                        sport_parlays=sport_parlays, first_pitch_utc=earliest_first_pitch)

    history = _build_history(db, date_str)
    report = DailyReport(
        date=date_str, slate_size=len(games), plays=plays, fade_teams=fade_teams, hr_props=hr_props, parlay=parlay,
        dropped_notes=dropped_notes, celestial=_celestial_dict(run_date),
        numerology=_numerology_dict(run_date), bankroll_summary=bankroll_summary(db, history),
        data_warnings=data_warnings, results_recap=_build_results_recap(db, date_str),
        history=history,
        daily_parlay=top_parlay,
        sport_parlays=sport_parlays, top_parlay=top_parlay, double_parlay=double_parlay,
        active_sports=active_sports,
        pick_changes=get_pick_changes(date_str),
    )
    _emit(report)
    if args.auto:
        auto_gate.mark_published(date_str)


def _build_history(db, today_str):
    """Past graded slates, newest first -- STRICTLY days before today_str.
    Dates through LEDGER_CUTOFF and any date in SEED_OVERRIDE_DATES come from
    the verified seed below; the DB supplies every other date after the cutoff."""
    seed = [
        {"date": "2026-07-30",
         "parlay": ["PIT ML (-112)", "TB ML (-178)", "ATL ML (-154)", "CWS ML (-116)"],
         "picks": [
            {"label": "TB ML (-178)", "status": "won", "kind": "moneyline"},
            {"label": "ATL ML (-154)", "status": "won", "kind": "moneyline"},
            {"label": "CWS ML (-116)", "status": "won", "kind": "moneyline"},
            {"label": "PIT ML (-112)", "status": "lost", "kind": "moneyline"},
            {"label": "MIA ML (+110)", "status": "lost", "kind": "moneyline"},
            {"label": "Munetaka Murakami to hit a HR", "status": "lost", "kind": "hr_prop"},
            {"label": "James Wood to hit a HR", "status": "lost", "kind": "hr_prop"},
            {"label": "Drake Baldwin to hit a HR", "status": "lost", "kind": "hr_prop"},
        ]},
        {"date": "2026-07-29",
         "parlay": ["TOR ML", "TB ML", "ATL (Gm 2) ML", "BOS ML"],
         "picks": [
            {"label": "TOR ML", "status": "won", "kind": "moneyline"},
            {"label": "TB ML", "status": "won", "kind": "moneyline"},
            {"label": "ATL (Gm 2) ML", "status": "won", "kind": "moneyline"},
            {"label": "BOS ML", "status": "won", "kind": "moneyline"},
            {"label": "Max Muncy to hit a HR", "status": "lost", "kind": "hr_prop"},
            {"label": "Kazuma Okamoto to hit a HR", "status": "lost", "kind": "hr_prop"},
            {"label": "James Wood to hit a HR", "status": "lost", "kind": "hr_prop"},
        ]},
        {"date": "2026-07-26",
         "parlay": ["BOS ML (-112)", "ARI ML (+102)", "MIL ML (-238)", "CWS ML (+109)"],
         "picks": [
            {"label": "BOS ML (-112)", "status": "won", "kind": "moneyline"},
            {"label": "MIL ML (-238)", "status": "won", "kind": "moneyline"},
            {"label": "CWS ML (+109)", "status": "won", "kind": "moneyline"},
            {"label": "MIN ML", "status": "won", "kind": "moneyline"},
            {"label": "ARI ML (+102)", "status": "lost", "kind": "moneyline"},
            {"label": "Pete Alonso to hit a HR (+280)", "status": "won", "kind": "hr_prop"},
            {"label": "Dominic Canzone to hit a HR", "status": "won", "kind": "hr_prop"},
            {"label": "Brandon Nimmo to hit a HR", "status": "lost", "kind": "hr_prop"},
        ]},
        {"date": "2026-07-25",
         "parlay": ["TB ML (-120)", "STL ML (-112)", "WSH ML (-134)", "ARI ML"],
         "picks": [
            {"label": "ARI ML", "status": "won", "kind": "moneyline"},
            {"label": "WSH ML (-134)", "status": "won", "kind": "moneyline"},
            {"label": "STL ML (-112)", "status": "won", "kind": "moneyline"},
            {"label": "TB ML (-120)", "status": "won", "kind": "moneyline"},
            {"label": "MIA ML (-142)", "status": "lost", "kind": "moneyline"},
            {"label": "Bryan De La Cruz to hit a HR", "status": "lost", "kind": "hr_prop"},
            {"label": "Christian Encarnacion-Strand to hit a HR", "status": "lost", "kind": "hr_prop"},
            {"label": "Dominic Canzone to hit a HR", "status": "lost", "kind": "hr_prop"},
        ]},
        {"date": "2026-07-24",
         "parlay": [],
         "picks": [
            {"label": "ARI ML (-124)", "status": "won", "kind": "moneyline"},
            {"label": "MIL ML (-122)", "status": "lost", "kind": "moneyline"},
            {"label": "MIN ML (-144)", "status": "lost", "kind": "moneyline"},
            {"label": "Christian Encarnacion-Strand to hit a HR (+450)", "status": "won", "kind": "hr_prop"},
            {"label": "Drake Baldwin to hit a HR (+422)", "status": "won", "kind": "hr_prop"},
            {"label": "Matt Olson to hit a HR (+310)", "status": "won", "kind": "hr_prop"},
        ]},
    ]
    by_date = {}
    order = []
    for r in db.get_graded_history(after=LEDGER_CUTOFF):
        d = r["date"]
        if d >= today_str:
            continue
        if d in SEED_OVERRIDE_DATES:
            continue
        if d not in by_date:
            by_date[d] = []
            order.append(d)
        if r["kind"] == "moneyline":
            odds = r["odds_american"]
            label = f"{r['team']} ML ({odds:+d})" if odds is not None else f"{r['team']} ML"
        elif r["kind"] == "hr_prop":
            odds = r["odds_american"]
            label = f"{r['side_or_player']} to hit a HR ({odds:+d})" if odds is not None else f"{r['side_or_player']} to hit a HR"
        else:
            continue
        by_date[d].append({"label": label, "status": r["status"], "kind": r["kind"]})
    db_days = []
    for d in order:
        parlay_rows = db.get_recommendations_for_date(d, kind="parlay_leg")
        db_days.append({"date": d, "picks": by_date[d],
                        "parlay": [r["side_or_player"] for r in parlay_rows]})
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
        if r["kind"] == "moneyline":
            odds = r["odds_american"]
            label = f"{r['team']} ML ({odds:+d})" if odds is not None else f"{r['team']} ML"
        elif r["kind"] == "hr_prop":
            odds = r["odds_american"]
            label = f"{r['side_or_player']} to hit a HR ({odds:+d})" if odds is not None else f"{r['side_or_player']} to hit a HR"
        else:
            continue
        items.append({"label": label, "status": r["status"], "kind": r["kind"]})
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


if __name__ == "__main__":
    main()
