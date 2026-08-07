"""
backtest/backtest_mlb.py
=========================
MLB backtest, DIRECTIONAL (free) + ROI (--use-odds, paid historical odds).

Look-ahead-safe: each team's win% is built from actual game finals in
chronological order, using only the record BEFORE each game -- never
season-final standings. That removes the bias where the model "knew" how good
a team turned out to be. Records accumulate from the range start, so early-
window games have thin records (the min-lean filter naturally skips those).

Run via the "MLB Backtest" button in Actions, or locally:
    python -m backtest.backtest_mlb --start 2025-04-01 --end 2025-09-28 --min-lean 0.06 --use-odds
"""

import argparse
import logging
import os
from collections import defaultdict
from datetime import date, timedelta

import requests

from data.celestial import moon_phase_for, moon_sign_for
from data.numerology import reduce_date

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backtest")

SCHEDULE_API = "https://statsapi.mlb.com/api/v1/schedule"
HIST_ODDS_API = "https://api.the-odds-api.com/v4/historical/sports/baseball_mlb/odds"

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


def _norm_team(name):
    return " ".join(str(name or "").lower().split())


def _american_profit(odds):
    odds = float(odds)
    return odds / 100.0 if odds > 0 else 100.0 / (-odds)


def _win_pct(rec):
    w, l = rec
    return (w / (w + l)) if (w + l) > 0 else None


def _model_lean(home_pct, away_pct, d):
    score = 0.0  # positive => home
    if home_pct is not None and away_pct is not None:
        score += (home_pct - away_pct) * 0.6
    score += 0.04  # home field
    phase, _ = moon_phase_for(d)
    element = SIGN_ELEMENT.get(moon_sign_for(d), "")
    fav_is_home = (home_pct >= away_pct) if (home_pct is not None and away_pct is not None) else True
    fav_dir = 1 if fav_is_home else -1
    nudge = PHASE_LEAN.get(phase, 0) + ELEMENT_LEAN.get(element, 0) + NUMBER_LEAN.get(reduce_date(d), 0)
    score += fav_dir * nudge * 0.01
    side = "home" if score >= 0 else "away"
    return side, abs(score)


def _final_games_for_date(d):
    """List of final games with ids, names, scores. []"""
    try:
        resp = requests.get(SCHEDULE_API, params={
            "sportId": 1, "date": d.strftime("%Y-%m-%d"), "hydrate": "team,linescore",
        }, timeout=20)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning("schedule fetch failed %s: %s", d, exc)
        return []
    out = []
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
            out.append({
                "home_id": g["teams"]["home"]["team"]["id"],
                "away_id": g["teams"]["away"]["team"]["id"],
                "home_name": _norm_team(g["teams"]["home"]["team"]["name"]),
                "away_name": _norm_team(g["teams"]["away"]["team"]["name"]),
                "winner": "home" if h > a else "away",
            })
    return out


def _historical_ml_for_date(d, api_key):
    ts = f"{d.strftime('%Y-%m-%d')}T20:00:00Z"
    params = {"apiKey": api_key, "regions": "us", "markets": "h2h",
              "bookmakers": "fanduel", "oddsFormat": "american", "date": ts}
    try:
        resp = requests.get(HIST_ODDS_API, params=params, timeout=25)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning("historical odds fetch failed %s: %s", d, exc)
        return {}
    events = payload.get("data", payload if isinstance(payload, list) else [])
    out = {}
    for ev in events:
        home = _norm_team(ev.get("home_team"))
        away = _norm_team(ev.get("away_team"))
        price = {}
        for bm in ev.get("bookmakers", []):
            if bm.get("key") != "fanduel":
                continue
            for market in bm.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                for o in market.get("outcomes", []):
                    if _norm_team(o.get("name")) == home:
                        price["home"] = o.get("price")
                    elif _norm_team(o.get("name")) == away:
                        price["away"] = o.get("price")
        if "home" in price and "away" in price:
            out[(home, away)] = price
    return out


def run_backtest(start, end, min_lean, use_odds=False):
    api_key = os.getenv("ODDS_API_KEY", "")
    if use_odds and not api_key:
        logger.warning("--use-odds set but ODDS_API_KEY empty -- directional only.")
        use_odds = False

    running = defaultdict(lambda: [0, 0])  # team_id -> [wins, losses] BEFORE current day
    buckets = defaultdict(lambda: {"n": 0, "wins": 0, "staked": 0.0, "won": 0.0})
    total = {"n": 0, "wins": 0, "staked": 0.0, "won": 0.0}
    dog = {"n": 0, "wins": 0, "staked": 0.0, "won": 0.0}
    graded = 0
    priced = 0

    for d in _daterange(start, end):
        games = _final_games_for_date(d)
        if not games:
            continue
        odds_map = _historical_ml_for_date(d, api_key) if use_odds else {}

        for g in games:
            home_pct = _win_pct(running[g["home_id"]])
            away_pct = _win_pct(running[g["away_id"]])
            side, strength = _model_lean(home_pct, away_pct, d)
            if strength >= min_lean:
                won = (g["winner"] == side)
                graded += 1
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
                if use_odds:
                    price = odds_map.get((g["home_name"], g["away_name"]))
                    my_ml = price.get(side) if price else None
                    if my_ml is not None:
                        priced += 1
                        profit = _american_profit(my_ml) if won else -1.0
                        for bag in (total, buckets[b]):
                            bag["staked"] += 1.0
                            bag["won"] += profit
                        if my_ml > 0:
                            dog["n"] += 1
                            dog["wins"] += int(won)
                            dog["staked"] += 1.0
                            dog["won"] += profit

        # update running records AFTER grading the whole day
        for g in games:
            if g["winner"] == "home":
                running[g["home_id"]][0] += 1
                running[g["away_id"]][1] += 1
            else:
                running[g["away_id"]][0] += 1
                running[g["home_id"]][1] += 1

    _report(start, end, min_lean, total, buckets, dog, graded, use_odds, priced)


def _roi_str(bag):
    if bag["staked"] <= 0:
        return ""
    roi = 100.0 * bag["won"] / bag["staked"]
    return f"  |  {bag['won']:+.1f}u  ROI {roi:+.1f}%"


def _report(start, end, min_lean, total, buckets, dog, graded, use_odds, priced):
    logger.info("\n==================== MLB BACKTEST (%s) ====================",
                "ROI + DIRECTIONAL" if use_odds else "DIRECTIONAL")
    logger.info("Range: %s -> %s   |   min lean filter: %.2f", start, end, min_lean)
    logger.info("Games graded: %d%s", graded,
                f"   |   priced by historical odds: {priced}" if use_odds else "")
    logger.info("Records are AS-OF each game date (look-ahead-safe).")
    logger.info("------------------------------------------------------------------")
    if total["n"]:
        logger.info("OVERALL: %d picks, %.1f%% win rate%s", total["n"],
                    100.0 * total["wins"] / total["n"], _roi_str(total))
    else:
        logger.info("No games cleared the lean filter in this range.")
    logger.info("------------------------------------------------------------------")
    logger.info("By lean strength:")
    for b in ["STRONG (>=0.15)", "MED (0.09-0.15)", "LEAN (< 0.09)"]:
        if b in buckets and buckets[b]["n"]:
            data = buckets[b]
            logger.info("  %-18s %4d picks  %.1f%% win%s", b, data["n"],
                        100.0 * data["wins"] / data["n"], _roi_str(data))
    if use_odds:
        logger.info("------------------------------------------------------------------")
        if dog["n"]:
            logger.info("UNDERDOG-ONLY (model backed the priced dog): %d picks, %.1f%% win%s",
                        dog["n"], 100.0 * dog["wins"] / dog["n"], _roi_str(dog))
        else:
            logger.info("UNDERDOG-ONLY: no dog picks cleared the filter in this range.")
        logger.info("------------------------------------------------------------------")
        logger.info("ROI is the real test: POSITIVE units = the model beat the closing price.")
    logger.info("==================================================================\n")


def main():
    p = argparse.ArgumentParser(description="MLB backtest (directional + optional ROI).")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--min-lean", type=float, default=0.0)
    p.add_argument("--use-odds", action="store_true")
    args = p.parse_args()
    run_backtest(date.fromisoformat(args.start), date.fromisoformat(args.end),
                 args.min_lean, use_odds=args.use_odds)


if __name__ == "__main__":
    main()
