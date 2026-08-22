"""
data/football_context.py
=========================
Football-specific context (NFL / NCAAF) built ONLY from data we already
store -- the `game_scores` table that data/standings_scores.py fills and
commits back to the repo every run. No new API, no scraping, nothing that
can be IP-blocked from GitHub Actions.

What it derives per team:
  rest_days      -- days since that team's last completed game. Real edge in
                    football: short rest (Thursday games, road back-to-backs)
                    measurably hurts, extra rest helps.
  last3_margin   -- average scoring margin over the last 3 games (recent form,
                    which matters far more in a 17-game season than in 162).
  season_margin  -- average scoring margin all season (baseline talent).
  form_delta     -- last3_margin minus season_margin. Positive = trending up,
                    negative = fading. This is what catches a team peaking or
                    collapsing before the market fully adjusts.
  home_margin /
  away_margin    -- scoring margin split by venue. Some teams are genuinely
                    different at home; football has the largest home-field
                    effect of the major sports.

PRESEASON HONESTY (is_preseason): NFL games in August are preseason, played
mostly by backups. Records, scoring margins and form from those games predict
almost nothing about the regular season. This module flags it so the grading
factor can say so plainly instead of pretending a preseason blowout is signal.
(NCAAF in late August IS real regular season -- week 0/1 -- so it is never
flagged.)

Never raises -- returns empty/neutral on any problem so the daily run
always finishes.
"""

import logging
import sqlite3
from datetime import datetime, date as date_cls

import config

logger = logging.getLogger(__name__)

FOOTBALL_SPORTS = {"NFL", "NCAAF"}


def is_preseason(sport, date_str):
    """NFL in August = preseason (backups play, results are noise)."""
    if sport != "NFL":
        return False
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return False
    return d.month == 8


def _conn():
    c = sqlite3.connect(str(config.DB_PATH))
    c.execute("""CREATE TABLE IF NOT EXISTS game_scores (
                   game_key TEXT PRIMARY KEY,
                   sport TEXT NOT NULL,
                   date TEXT NOT NULL,
                   home_team TEXT NOT NULL,
                   away_team TEXT NOT NULL,
                   home_pts REAL NOT NULL,
                   away_pts REAL NOT NULL)""")
    return c


def _team_games(sport, team, season):
    """All stored final games for a team this season, oldest first."""
    try:
        c = _conn()
        rows = c.execute(
            "SELECT date, home_team, away_team, home_pts, away_pts FROM game_scores "
            "WHERE sport=? AND substr(date,1,4)=? AND (home_team=? OR away_team=?) "
            "ORDER BY date ASC",
            (sport, str(season), team, team)).fetchall()
        c.close()
    except Exception as exc:
        logger.debug("football_context read failed (%s %s): %s", sport, team, exc)
        return []
    out = []
    for d, home, away, hp, ap in rows:
        at_home = (home == team)
        own = hp if at_home else ap
        opp = ap if at_home else hp
        out.append({"date": d, "at_home": at_home, "margin": own - opp})
    return out


def _avg(vals):
    return round(sum(vals) / len(vals), 2) if vals else None


def team_football_context(sport, team, date_str, season=None):
    """Returns the context dict for one team. Empty dict if we have no games
    stored yet (early season) -- callers treat that as neutral."""
    if sport not in FOOTBALL_SPORTS:
        return {}
    season = season or (date_str[:4] if date_str else datetime.now().year)
    games = _team_games(sport, team, season)
    if not games:
        return {"games": 0}

    margins = [g["margin"] for g in games]
    last3 = margins[-3:]
    home_margins = [g["margin"] for g in games if g["at_home"]]
    away_margins = [g["margin"] for g in games if not g["at_home"]]

    rest_days = None
    try:
        today = datetime.strptime(date_str, "%Y-%m-%d").date()
        last = datetime.strptime(games[-1]["date"], "%Y-%m-%d").date()
        rest_days = (today - last).days
    except Exception:
        pass

    season_margin = _avg(margins)
    last3_margin = _avg(last3)
    form_delta = (round(last3_margin - season_margin, 2)
                  if last3_margin is not None and season_margin is not None else None)

    return {
        "games": len(games),
        "rest_days": rest_days,
        "season_margin": season_margin,
        "last3_margin": last3_margin,
        "form_delta": form_delta,
        "home_margin": _avg(home_margins),
        "away_margin": _avg(away_margins),
    }


def matchup_football_context(sport, home_team, away_team, date_str, season=None):
    """Both sides plus the preseason flag, ready for the grading factor."""
    if sport not in FOOTBALL_SPORTS:
        return {}
    return {
        "sport": sport,
        "preseason": is_preseason(sport, date_str),
        "home": team_football_context(sport, home_team, date_str, season),
        "away": team_football_context(sport, away_team, date_str, season),
    }
