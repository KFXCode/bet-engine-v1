"""
backtest/backtest_mlb.py
=========================
FREE directional backtest for MLB. Answers one question with real history:
when the model leans a side, how often does that side actually win -- and
does a STRONGER lean mean a HIGHER win rate?

FAST version: final scores are pulled in BULK from the schedule endpoint
(one request per DAY, ~115 total for a season) instead of one slow feed/live
call per game. A whole season runs in a couple of minutes.

What it does NOT do (yet): true edge / ROI / bankroll. That needs the
historical MONEYLINE ODDS as they were on each past date -- a paid Odds API
add-on. Until then this validates the model's DIRECTION only, using the fast
always-available factors (season records, home field, moon, numerology).

Run via the "MLB Backtest" button in the Actions tab, or locally:
    python -m backtest.backtest_mlb --start 2026-04-01 --end 2026-07-23 --min-lean 0.06
"""

import argparse
import logging
from collections import defaultdict
from datetime import date, timedelta

import requests

from data.celestial import moon_phase_for, moon_sign_for
from data.numerology import reduce_date

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backtest")

SCHEDULE_API = "https://statsapi.mlb.com/api/v1/schedule"
STANDINGS_API = "https://statsapi.mlb.com/api/v1/standings"

REAL_GAME_TYPES = {"R", "F", "D", "L", "W"}

PHASE_LEAN = {
    "New Moon": +1, "Waxing Crescent": +1, "First Quarter": 0,
    "Waxing Gibbous": -1, "Full Moon": -1, "Waning Gibbous": -1,
    "Last Quarter": 0, "Waning Crescent": +1,
}
SIGN_ELEMENT = {
    "Aries": "fire", "Leo": "fire", "Sagittarius": "fire",
    "Taurus": "earth", "Virgo": "earth", "Capricorn": "earth",
    "Gemini": "air", "Libra": "air", "Aquarius": "air",
    "Cancer": "water", "Scorpio": "water", "Pisces": "water",
}
ELEMENT_LEAN = {"fire": +1, "earth": +1, "air": -1, "water": -1}
NUMBER_LEAN = {1: +1, 2: 0, 3: +1, 4: -1, 5: -1, 6: +1, 7: 0, 8: -1, 9: +1, 11: +1, 22: -1, 33: +1}


def _daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _season_records(year):
    records = {}
    try:
        resp = requests.get(STANDINGS_API, params={
            "leagueId": "103,104", "season": year, "standingsTypes": "regularSeason",
        }, timeout=20)
        resp.raise_for_status()
        for rec in resp.json().get("records", []):
            for tr in rec.get("teamRecords", []):
                tid = tr["team"]["id"]
                pct = float(tr.get("winningPercentage", 0) or 0)
                records[tid] = (tr.get("wins", 0), tr.get("losses", 0), pct)
    except Exception as exc:
        logger.warning("standings fetch failed for %s: %s", year, exc)
    return records


def _model_lean(home_id, away_id, records, d):
    score = 0.0  # positive => home
    hw = records.get(home_id)
    aw = records.get(away_id)
    if hw and aw:
        score += (hw[2] - aw[2]) * 0.6
    score += 0.04  # home field
    phase, _ = moon_phase_for(d)
    element = SIGN_ELEMENT.get(moon_sign_for(d), "")
    fav_is_home = (hw[2] >= aw[2]) if (hw and aw) else True
    fav_dir = 1 if fav_is_home else -1
    nudge = PHASE_LEAN.get(phase, 0) + ELEMENT_LEAN.get(element, 0) + NUMBER_LEAN.get(reduce_date(d), 0)
    score += fav_dir * nudge * 0.01
    side = "home" if score >= 0 else "away"
    return side, abs(score)


def run_backtest(start, end, min_lean):
    years = {start.year, end.year}
    records_by_year = {y: _season_records(y) for y in years}

    buckets = defaultdict(lambda: {"n": 0, "wins": 0})
    total = {"n": 0, "wins": 0}
    graded_games = 0

    for d in _daterange(start, end):
        # ONE request per day, with final scores already hydrated in linescore.
        try:
            resp = requests.get(SCHEDULE_API, params={
                "sportId": 1, "date": d.strftime("%Y-%m-%d"), "hydrate": "team,linescore",
            }, timeout=20)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.warning("schedule fetch failed %s: %s", d, exc)
            continue

        records = records_by_year.get(d.year, {})
        for block in payload.get("dates", []):
            for g in block.get("games", []):
                if g.get("gameType") not in REAL_GAME_TYPES:
                    continue
                if g.get("status", {}).get("abstractGameState") != "Final":
                    continue
                ls = g.get("linescore", {}).get("teams", {})
                h = ls.get("home", {}).get("runs")
                a = ls.get("away", {}).get("runs")
                if h is None or a is None or h == a:
                    continue

                home_id = g["teams"]["home"]["team"]["id"]
                away_id = g["teams"]["away"]["team"]["id"]
                side, strength = _model_lean(home_id, away_id, records, d)
                if strength < min_lean:
                    continue

                winner = "home" if h > a else "away"
                won = (winner == side)
                graded_games += 1
                total["n"] += 1
                total["wins"] += int(won)
                if strength >= 0.15:
                    b = "STRONG (>=0.15)"
                elif strength >= 0.09:
                    b = "MED (0.09-0.15)"
                else:
                    b = "LEAN (< 0.09)"
                buckets[b]["n"] += 1
                buckets[b]["wins"] += int(won)

    _report(start, end, min_lean, total, buckets, graded_games)


def _report(start, end, min_lean, total, buckets, graded_games):
    logger.info("\n==================== MLB DIRECTIONAL BACKTEST ====================")
    logger.info("Range: %s -> %s   |   min lean filter: %.2f", start, end, min_lean)
    logger.info("Games graded: %d", graded_games)
    logger.info("------------------------------------------------------------------")
    if total["n"]:
        wr = 100.0 * total["wins"] / total["n"]
        logger.info("OVERALL: %d picks, %d wins  =>  %.1f%% win rate", total["n"], total["wins"], wr)
    else:
        logger.info("No games cleared the lean filter in this range.")
    logger.info("------------------------------------------------------------------")
    logger.info("By lean strength (does stronger lean = higher win rate?):")
    for b in ["STRONG (>=0.15)", "MED (0.09-0.15)", "LEAN (< 0.09)"]:
        if b in buckets and buckets[b]["n"]:
            data = buckets[b]
            logger.info("  %-18s %4d picks  %.1f%% win rate", b, data["n"], 100.0 * data["wins"] / data["n"])
    logger.info("------------------------------------------------------------------")
    logger.info("NOTE: directional only -- no odds, so this is win RATE, not ROI.")
    logger.info("A ~53-56%%+ rate on the STRONG bucket is a healthy directional edge.")
    logger.info("==================================================================\n")


def main():
    p = argparse.ArgumentParser(description="Free MLB directional backtest.")
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD")
    p.add_argument("--min-lean", type=float, default=0.0)
    args = p.parse_args()
    run_backtest(date.fromisoformat(args.start), date.fromisoformat(args.end), args.min_lean)


if __name__ == "__main__":
    main()
