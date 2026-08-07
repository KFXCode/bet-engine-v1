"""
backtest/backtest_multi.py
===========================
Directional (+ optional ROI) backtest for NFL, NCAAF, NCAAB, NHL, NBA.

Look-ahead-safe: team win% is built from actual game finals in chronological
order (record BEFORE each game), NOT ESPN's displayed record or any
end-of-season figure. Scores come from ESPN's free scoreboard; with
--use-odds it prices picks from The Odds API historical closing lines for
UNITS/ROI plus an underdog-only cut.

Run via the "Multi-Sport Backtest" button in Actions, or locally:
    python -m backtest.backtest_multi --sport NBA --start 2024-10-22 --end 2025-04-13 --min-lean 0.06 --use-odds
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

SPORTS = {
    "NFL": ("football/nfl", "americanfootball_nfl"),
    "NCAAF": ("football/college-football", "americanfootball_ncaaf"),
    "NCAAB": ("basketball/mens-college-basketball", "basketball_ncaab"),
    "NHL": ("hockey/nhl", "icehockey_nhl"),
    "NBA": ("basketball/nba", "basketball_nba"),
}
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"
HIST_ODDS_API = "https://api.the-odds-api.com/v4/historical/sports/{key}/odds"

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
    score = 0.0
    if home_pct is not None and away_pct is not None:
        score += (home_pct - away_pct) * 0.6
    score += 0.04
    phase, _ = moon_phase_for(d)
    element = SIGN_ELEMENT.get(moon_sign_for(d), "")
    fav_is_home = (home_pct >= away_pct) if (home_pct is not None and away_pct is not None) else True
    fav_dir = 1 if fav_is_home else -1
    nudge = PHASE_LEAN.get(phase, 0) + ELEMENT_LEAN.get(element, 0) + NUMBER_LEAN.get(reduce_date(d), 0)
    score += fav_dir * nudge * 0.01
    side = "home" if score >= 0 else "away"
    return side, abs(score)


def _espn_games_for_date(path, d):
    """Completed games: home/away name + winner. No record fields (we compute
    records ourselves, look-ahead-safe)."""
    try:
        resp = requests.get(ESPN_SCOREBOARD.format(path=path),
                            params={"dates": d.strftime("%Y%m%d"), "limit": 400}, timeout=25)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning("ESPN scoreboard failed %s %s: %s", path, d, exc)
        return []
    games = []
    for ev in payload.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        status = comp.get("status", {}).get("type", {})
        if not status.get("completed"):
            continue
        competitors = comp.get("competitors", [])
        if len(competitors) != 2:
            continue
        rec = {"home": None, "away": None}
        ok = True
        for c in competitors:
            ha = c.get("homeAway")
            if ha not in ("home", "away"):
                ok = False
                break
            try:
                sc = int(c.get("score"))
            except (TypeError, ValueError):
                ok = False
                break
            rec[ha] = {"name": _norm_team((c.get("team") or {}).get("displayName")), "score": sc}
        if not ok or not rec["home"] or not rec["away"]:
            continue
        if rec["home"]["score"] == rec["away"]["score"]:
            continue
        rec["winner"] = "home" if rec["home"]["score"] > rec["away"]["score"] else "away"
        games.append(rec)
    return games


def _historical_ml_for_date(sport_key, d, api_key):
    ts = f"{d.strftime('%Y-%m-%d')}T23:00:00Z"
    params = {"apiKey": api_key, "regions": "us", "markets": "h2h",
              "bookmakers": "fanduel", "oddsFormat": "american", "date": ts}
    try:
        resp = requests.get(HIST_ODDS_API.format(key=sport_key), params=params, timeout=25)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning("historical odds fetch failed %s %s: %s", sport_key, d, exc)
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


def run_backtest(sport, start, end, min_lean, use_odds=False):
    path, sport_key = SPORTS[sport]
    api_key = os.getenv("ODDS_API_KEY", "")
    if use_odds and not api_key:
        logger.warning("--use-odds set but ODDS_API_KEY empty -- directional only.")
        use_odds = False

    running = defaultdict(lambda: [0, 0])  # team name -> [wins, losses] BEFORE current day
    buckets = defaultdict(lambda: {"n": 0, "wins": 0, "staked": 0.0, "won": 0.0})
    total = {"n": 0, "wins": 0, "staked": 0.0, "won": 0.0}
    dog = {"n": 0, "wins": 0, "staked": 0.0, "won": 0.0}
    graded = 0
    priced = 0

    for d in _daterange(start, end):
        games = _espn_games_for_date(path, d)
        if not games:
            continue
        odds_map = _historical_ml_for_date(sport_key, d, api_key) if use_odds else {}

        for g in games:
            home_name = g["home"]["name"]
            away_name = g["away"]["name"]
            home_pct = _win_pct(running[home_name])
            away_pct = _win_pct(running[away_name])
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
                    price = odds_map.get((home_name, away_name))
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

        for g in games:
            if g["winner"] == "home":
                running[g["home"]["name"]][0] += 1
                running[g["away"]["name"]][1] += 1
            else:
                running[g["away"]["name"]][0] += 1
                running[g["home"]["name"]][1] += 1

    _report(sport, start, end, min_lean, total, buckets, dog, graded, use_odds, priced)


def _roi_str(bag):
    if bag["staked"] <= 0:
        return ""
    roi = 100.0 * bag["won"] / bag["staked"]
    return f"  |  {bag['won']:+.1f}u  ROI {roi:+.1f}%"


def _report(sport, start, end, min_lean, total, buckets, dog, graded, use_odds, priced):
    logger.info("\n============ %s BACKTEST (%s) ============",
                sport, "ROI + DIRECTIONAL" if use_odds else "DIRECTIONAL")
    logger.info("Range: %s -> %s   |   min lean: %.2f", start, end, min_lean)
    logger.info("Games graded: %d%s", graded, f"   |   priced: {priced}" if use_odds else "")
    logger.info("Records are AS-OF each game date (look-ahead-safe).")
    logger.info("------------------------------------------------------------------")
    if total["n"]:
        logger.info("OVERALL: %d picks, %.1f%% win rate%s", total["n"],
                    100.0 * total["wins"] / total["n"], _roi_str(total))
    else:
        logger.info("No games cleared the lean filter (check dates are IN-SEASON for %s).", sport)
    logger.info("------------------------------------------------------------------")
    for b in ["STRONG (>=0.15)", "MED (0.09-0.15)", "LEAN (< 0.09)"]:
        if b in buckets and buckets[b]["n"]:
            data = buckets[b]
            logger.info("  %-18s %4d picks  %.1f%% win%s", b, data["n"],
                        100.0 * data["wins"] / data["n"], _roi_str(data))
    if use_odds and dog["n"]:
        logger.info("------------------------------------------------------------------")
        logger.info("UNDERDOG-ONLY: %d picks, %.1f%% win%s", dog["n"],
                    100.0 * dog["wins"] / dog["n"], _roi_str(dog))
    logger.info("==================================================================\n")


def main():
    p = argparse.ArgumentParser(description="Multi-sport backtest (NFL/NCAAF/NCAAB/NHL/NBA).")
    p.add_argument("--sport", required=True, choices=list(SPORTS.keys()))
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--min-lean", type=float, default=0.0)
    p.add_argument("--use-odds", action="store_true")
    args = p.parse_args()
    run_backtest(args.sport, date.fromisoformat(args.start), date.fromisoformat(args.end),
                 args.min_lean, use_odds=args.use_odds)


if __name__ == "__main__":
    main()
