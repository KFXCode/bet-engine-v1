"""
engine/td_props.py
===================
Anytime-touchdown-scorer props for NFL, the football counterpart to
engine/hr_props.py.

WHY POISSON INSTEAD OF A POINTS PILE: scoring a TD is a rare count event, the
same shape as hitting a home run, so the honest model is
    P(at least one TD) = 1 - exp(-lambda)
where lambda is the player's expected TDs in THIS game. That gives real
probabilities we can compare against a sportsbook price -- an elite back at
lambda 0.8 comes out ~55%, a rotational WR at 0.15 comes out ~14%, which is
what those props actually pay. Building a 0-100 points pile first and
back-fitting a probability (the early HR approach) produced numbers that
looked precise but meant nothing.

lambda is built from three real inputs:
  1. BASELINE  -- the player's season TD/game (data/nfl_players.get_td_profile;
     falls back to last completed season in preseason/Week 1).
  2. GAME ENVIRONMENT -- the player's team IMPLIED TOTAL, derived from the
     market's game total and spread. This is the single most useful public
     signal for TD props: a team projected for 28 points scores far more TDs
     than one projected for 16, and the market tells us that for free.
  3. ROLE -- goal-line backs and target-hog receivers convert opportunity into
     TDs at different rates, so volume (carries/targets) nudges lambda.

The 0-100 score shown in the report is derived FROM the probability (not the
other way round), purely so the board can be ranked and tiered consistently
with the HR board.
"""

import logging
import math
import re
import unicodedata

import config

logger = logging.getLogger("td_props")

# Tunables (config can override any of these without touching this file).
MAX_PER_DAY = getattr(config, "TD_PROP_MAX_PER_DAY", 3)
STRONG_SCORE = getattr(config, "TD_PROP_STRONG_SCORE", 70)
MIN_EV_EDGE = getattr(config, "TD_MIN_EV_EDGE", 0.05)
MIN_LAMBDA = getattr(config, "TD_MIN_LAMBDA", 0.06)

LEAGUE_AVG_TEAM_TOTAL = 22.5     # points; the neutral environment
POSITION_CEILING = {"RB": 1.10, "FB": 0.55, "TE": 0.85, "WR": 0.95, "QB": 0.55}


def _norm(name):
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    n = re.sub(r"[.\,']", "", n)
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


def american_to_implied(ml):
    ml = float(ml)
    return 100.0 / (ml + 100.0) if ml > 0 else -ml / (-ml + 100.0)


def implied_team_totals(odds):
    """(home_total, away_total) from the market's total + spread, or (None, None).

    home_spread is negative when home is favored, so the favorite's implied
    total is total/2 + abs(spread)/2. This is standard, and it is what lets a
    TD prop know it's in a 49-point shootout vs a 37-point slog."""
    total = getattr(odds, "total", None)
    home_spread = getattr(odds, "home_spread", None)
    if total is None:
        return None, None
    try:
        total = float(total)
    except (TypeError, ValueError):
        return None, None
    if home_spread is None:
        return total / 2.0, total / 2.0
    try:
        hs = float(home_spread)
    except (TypeError, ValueError):
        return total / 2.0, total / 2.0
    home = (total / 2.0) - (hs / 2.0)
    away = total - home
    return round(home, 2), round(away, 2)


def _lambda_for(profile, position, team_implied_total):
    """Expected TDs for this player in this game, plus the reasoning trail."""
    reasoning = []
    games = profile.get("games") or 0
    base = profile.get("td_per_game") or 0.0
    season = profile.get("season")

    if games < 4:
        reasoning.append(f"[Baseline thin] Only {games} game(s) of TD history -- treated cautiously.")
    reasoning.append(
        f"[Baseline {base:.2f} TD/gm] {profile.get('total_td', 0)} TD in {games} game(s)"
        f"{f' ({season} season)' if season else ''} "
        f"({profile.get('rush_td', 0)} rush / {profile.get('rec_td', 0)} rec).")

    lam = base

    # Volume nudge: real opportunity, not just past finishes.
    touches = profile.get("touches") or 0
    per_game_touches = (touches / games) if games else 0
    if position in ("RB", "FB"):
        if per_game_touches >= 15:
            lam *= 1.12
            reasoning.append(f"[Workhorse +12%] {per_game_touches:.1f} touches/gm -- goal-line share likely.")
        elif per_game_touches < 6 and games >= 4:
            lam *= 0.85
            reasoning.append(f"[Low usage -15%] Only {per_game_touches:.1f} touches/gm -- limited scoring chances.")
    else:
        targets = profile.get("targets") or 0
        per_game_targets = (targets / games) if games else 0
        if per_game_targets >= 7:
            lam *= 1.10
            reasoning.append(f"[Target hog +10%] {per_game_targets:.1f} targets/gm.")
        elif per_game_targets < 2.5 and games >= 4:
            lam *= 0.85
            reasoning.append(f"[Low targets -15%] Only {per_game_targets:.1f} targets/gm.")

    # Game environment: the market's own read on how many points this team scores.
    if team_implied_total:
        factor = team_implied_total / LEAGUE_AVG_TEAM_TOTAL
        factor = max(0.70, min(1.35, factor))
        lam *= factor
        if factor >= 1.10:
            reasoning.append(f"[Environment +{(factor-1)*100:.0f}%] Team implied for "
                             f"{team_implied_total:.1f} pts -- well above the {LEAGUE_AVG_TEAM_TOTAL} average.")
        elif factor <= 0.90:
            reasoning.append(f"[Environment {(factor-1)*100:.0f}%] Team implied for only "
                             f"{team_implied_total:.1f} pts -- limited scoring expected.")
        else:
            reasoning.append(f"[Environment neutral] Team implied for {team_implied_total:.1f} pts.")
    else:
        reasoning.append("[Environment n/a] No game total posted yet -- scored on baseline and role only.")

    ceiling = POSITION_CEILING.get(position, 0.90)
    if lam > ceiling:
        lam = ceiling
        reasoning.append(f"[Capped] Held to {ceiling:.2f} expected TD -- the realistic ceiling for a {position}.")

    return max(0.0, lam), reasoning


def evaluate_td_candidates(games, rosters_by_team, td_profiles, odds_by_game):
    """rosters_by_team: {team_abbr: [{player_id, name, position}]}
    td_profiles:      {player_id: profile dict from data/nfl_players}
    odds_by_game:     {game_id: MoneylineOdds}
    Returns the full scored pool, sorted strongest first."""
    pool = []
    considered = 0
    skipped_no_profile = 0

    for game in games:
        if game.sport != "NFL":
            continue
        odds = odds_by_game.get(game.game_id)
        home_total, away_total = implied_team_totals(odds) if odds else (None, None)

        for team, opponent, team_total in (
            (game.home_team, game.away_team, home_total),
            (game.away_team, game.home_team, away_total),
        ):
            for player in rosters_by_team.get(team, []):
                considered += 1
                profile = td_profiles.get(player["player_id"])
                if not profile:
                    skipped_no_profile += 1
                    continue

                lam, reasoning = _lambda_for(profile, player["position"], team_total)
                if lam < MIN_LAMBDA:
                    continue

                prob = 1.0 - math.exp(-lam)
                score = round(min(100.0, prob * 145.0), 1)
                reasoning.insert(0,
                    f"Model: {lam:.2f} expected TD -> {prob*100:.1f}% chance to score "
                    f"(Poisson, 1 - e^-lambda).")
                reasoning.append(f"FINAL TD SCORE: {score}/100.")

                pool.append({
                    "player_name": player["name"],
                    "position": player["position"],
                    "team": team,
                    "opponent": opponent,
                    "game_id": game.game_id,
                    "score": score,
                    "model_prob": prob,
                    "expected_td": round(lam, 3),
                    "reasoning": reasoning,
                })

    pool.sort(key=lambda c: c["score"], reverse=True)
    logger.info("TD-DIAG: %d players considered, %d had no TD profile, %d scored into the pool.",
                considered, skipped_no_profile, len(pool))
    if pool:
        logger.info("TD-DIAG: top of pool -- %s",
                    ", ".join(f"{c['player_name']}({c['team']}) {c['model_prob']*100:.0f}%"
                              for c in pool[:6]))
    return pool


def finalize_td_props(pool, max_per_day=None, recent_players=None):
    """One ranked board: +EV tag where a price exists, one pick per game, and
    a rotation fade so the same three names don't repeat every week."""
    max_per_day = max_per_day or MAX_PER_DAY
    cold = recent_players or set()

    for c in pool:
        odds = c.get("odds_american")
        if odds is None:
            c["ev_edge"] = None
            c["reasoning"].append("[Value] No anytime-TD price available right now -- ranked on model "
                                  "probability; confirm the number before betting.")
            continue
        implied = american_to_implied(odds)
        edge = c["model_prob"] - implied
        c["ev_edge"] = edge
        c["implied_prob"] = implied
        if edge >= MIN_EV_EDGE:
            c["reasoning"].append(
                f"[+EV +{edge*100:.1f}%] Model {c['model_prob']*100:.1f}% vs book implied "
                f"{implied*100:.1f}% at {odds:+d} -- genuine betting value.")
        else:
            c["reasoning"].append(
                f"[Fair price {edge*100:+.1f}%] Model {c['model_prob']*100:.1f}% vs implied "
                f"{implied*100:.1f}% at {odds:+d} -- efficient price, a SPOT play not a value play.")

    fresh = [c for c in pool if _norm(c["player_name"]) not in cold]
    stale = [c for c in pool if _norm(c["player_name"]) in cold]
    for c in stale:
        c["reasoning"].append("[Cooldown] Picked recently without scoring -- shown only if the "
                              "fresh board can't fill the slate.")

    final, used_games, deferred = [], set(), []
    for c in fresh:
        if c["game_id"] in used_games:
            deferred.append(c)
            continue
        final.append(c)
        used_games.add(c["game_id"])
        if len(final) >= max_per_day:
            break
    if len(final) < max_per_day:
        for c in deferred + stale:
            if c in final:
                continue
            final.append(c)
            if len(final) >= max_per_day:
                break

    logger.info("TD-DIAG: FINAL %d pick(s): %s", len(final),
                ", ".join(f"{c['player_name']} {c['model_prob']*100:.0f}%"
                          + (f" EV{c['ev_edge']*100:+.1f}%" if c.get("ev_edge") is not None else " noodds")
                          for c in final) or "(none)")
    return final
