#!/usr/bin/env python3
"""
run_daily.py -- the one command you run each day.
"""

import argparse
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import auto_gate
import config
from data.db import Database
from data.schedule_provider import get_todays_games
from data.schedule_provider_wnba import get_todays_wnba_games
from data.odds_providers import get_odds_provider
from data.public_betting_provider import get_public_betting_provider
from data.stats_provider import get_stats_provider
from data.situational import park_and_situational_summary, ensure_injury_template, team_situational_summary
from data.standings import get_all_team_records
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
from engine.hr_props import evaluate_hr_prop_candidates
from engine.parlay import maybe_build_parlay, build_daily_parlay
from engine.models import DailyReport, ProbablePitcher

from output.terminal_report import print_daily_report
from output.html_report import render_daily_report
from output.history_log import log_recommendations, bankroll_summary
from output.publish_github_pages import publish_latest_report

from backtest.grader import grade_pending

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, logging.INFO),
                     format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("run_daily")

LEDGER_CUTOFF = "2026-07-26"  # dates through here come from the verified seed below, not the DB

# Individual later dates whose DB rows were also contaminated (a pre-first-pitch
# early run got frozen before the slate-lock fix). These are taken from the
# verified seed and SKIPPED from the DB so there's no duplicate.
SEED_OVERRIDE_DATES = {"2026-07-29"}


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
        if sport == "MLB":
            games.extend(get_todays_games(date_str))
        elif sport == "WNBA":
            games.extend(get_todays_wnba_games(date_str))

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
        report = DailyReport(date=date_str, slate_size=0, plays=[], fade_teams=[], hr_props=[], parlay=None,
                              dropped_notes=[], celestial=_celestial_dict(run_date),
                              numerology=_numerology_dict(run_date),
                              bankroll_summary=bankroll_summary(db),
                              data_warnings=["No games on today's schedule across enabled sports."],
                              results_recap=_build_results_recap(db, date_str),
                              history=_build_history(db, date_str))
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
    team_records = get_all_team_records()
    if not team_records:
        data_warnings.append("Standings unavailable today -- talent gap & motivation factors are running blind.")

    evaluations = []
    for game in games:
        odds = odds_by_game.get(game.game_id)
        if not odds:
            continue
        is_mlb = game.sport == "MLB"
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
            team_records.get(game.home_team, {}) if is_mlb else {},
            team_records.get(game.away_team, {}) if is_mlb else {},
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
        hr_props = evaluate_hr_prop_candidates(mlb_games, rosters, stats_provider, {},
                                                situational_by_team, lineup_source)
        hr_odds = fetch_hr_odds(hr_props, games)
        for prop in hr_props:
            key = (prop.get("game_id"), _norm_player(prop["player_name"]))
            prop["odds_american"] = hr_odds.get(key)

    raw_celestial, _, _ = celestial_signal_for(run_date)
    raw_numerology, _, _ = numerology_signal_for(run_date)
    parlay_pool = get_parlay_pool(evaluations)
    parlay = maybe_build_parlay(parlay_pool, raw_celestial, raw_numerology)
    daily_parlay = build_daily_parlay(plays, hr_props)

    # Earliest first pitch today -> history_log uses it to allow pre-game
    # refinement (each re-run replaces the slate) but LOCK once games start,
    # so the final pre-first-pitch state is what's saved -- not a stale
    # early-morning run, and not a post-game rewrite.
    first_pitches = [g.game_time_utc for g in games if g.game_time_utc]
    earliest_first_pitch = min(first_pitches) if first_pitches else None

    log_recommendations(db, date_str, plays, hr_props, daily_parlay, first_pitch_utc=earliest_first_pitch)

    report = DailyReport(
        date=date_str, slate_size=len(games), plays=plays, fade_teams=fade_teams, hr_props=hr_props, parlay=parlay,
        dropped_notes=dropped_notes, celestial=_celestial_dict(run_date),
        numerology=_numerology_dict(run_date), bankroll_summary=bankroll_summary(db),
        data_warnings=data_warnings, results_recap=_build_results_recap(db, date_str),
        history=_build_history(db, date_str),
        daily_parlay=daily_parlay,
    )
    _emit(report)
    if args.auto:
        auto_gate.mark_published(date_str)


def _build_history(db, today_str):
    """Past graded slates, newest first -- STRICTLY days before today_str.
    Dates through LEDGER_CUTOFF and any date in SEED_OVERRIDE_DATES come from
    the verified seed below (their DB rows were contaminated by pre-slate-lock
    early runs); the DB supplies every other date after the cutoff."""
    seed = [
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
            continue  # never show today (or future) in History -- only completed past slates
        if d in SEED_OVERRIDE_DATES:
            continue  # verified seed owns this date -- skip contaminated DB rows
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
    # Merge DB days (after cutoff, before today, excluding overrides) with the
    # seed, newest first.
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
