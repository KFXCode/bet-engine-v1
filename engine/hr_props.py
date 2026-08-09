"""
engine/hr_props.py
===================
HR Prop Workflow (runs automatically every day alongside moneyline):
  1. Barrel Signal Check       -- batter's own barrel%/recent trend
  2. Pitcher Vulnerability     -- opposing SP's barrel%/hard-hit%/HR-9 allowed
  3. Park + Motivation Overlay -- HR park factor + motivation context
  4. Public Lean Filter        -- fade extremely public props unless elite
  5. +EV Edge Filter           -- our probability vs FanDuel implied
  6. Cooldown + Diversification-- fade chronic recent-missers, one pick/game

evaluate_hr_prop_candidates() returns the FULL scored pool (sorted best-first,
NOT truncated). run_daily then attaches live FanDuel HR odds and calls
finalize_hr_props(), which applies the +EV edge filter, the cold-streak
cooldown, one-pick-per-game diversification, and the final cut.

SCORING PHILOSOPHY (rebalanced Aug 2026): homers are driven by the SPOT, not
the name. Per-game signals (recent barrel form, pitcher vulnerability, park)
carry the most weight; season power is a supporting factor; the season-HR floor
keeps legit power in and junk out.

COOLDOWN (added Aug 2026): the model is deterministic, so without memory the
same slugger surfaces every day. Any batter who was picked 2+ times in the last
7 days and did NOT homer is treated as "cold" and faded to the bottom of the
board so fresh value bats (the Day-1 3/3 profile) lead instead. A cold name only
returns to the top when there aren't enough fresh picks to fill the slate.

DIVERSIFICATION: at most one HR pick per game, so the board spreads across the
slate (Day 1 was three different teams) instead of stacking one game.

Ranking: +EV picks lead by edge; then fresh (non-cold) picks by score; cold
picks last. MLB-only. Slate is never empty.

DIAGNOSTICS: every batter considered is logged with the exact keep/drop reason.
Read it in the GitHub Actions run log under "HR-DIAG".
"""

import logging
import re as _re
import unicodedata as _ud

import config
from data.park_factors import park_factor_for

logger = logging.getLogger("hr_props")


def _norm_hr(name):
    """Normalize a batter name for matching against the recent-miss set
    (strip accents, punctuation, Jr/Sr suffixes, case)."""
    if not name:
        return ""
    n = _ud.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    n = _re.sub(r"[.\,']", "", n)
    n = _re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", n)
    return _re.sub(r"\s+", " ", n).strip()


def score_to_probability(score):
    """Map a 0-100 HR score to an estimated true HR probability for the game."""
    p = config.HR_PROB_BASE + (score - 50) * config.HR_PROB_PER_POINT
    return max(config.HR_PROB_MIN, min(config.HR_PROB_MAX, p))


def american_to_implied(ml):
    ml = float(ml)
    return 100.0 / (ml + 100.0) if ml > 0 else -ml / (-ml + 100.0)


def evaluate_hr_prop_candidates(games, rosters, stats_provider, public_prop_splits,
                                 situational_by_team, lineup_source=None):
    """Returns the FULL scored candidate pool (sorted, not truncated).
    finalize_hr_props() does the +EV filter, cooldown, and final cut."""
    lineup_source = lineup_source or {}
    candidates = []
    considered = 0
    dropped = {"no_data": [], "below_hr_floor": [], "public_fade": []}

    for game in games:
        for batting_team, opp_pitcher in (
            (game.home_team, game.away_pitcher),
            (game.away_team, game.home_pitcher),
        ):
            if not opp_pitcher:
                continue
            pitcher_profile = stats_provider.get_pitcher_profile(opp_pitcher.name)
            hr_park_factor = park_factor_for(game.home_team)[1]
            motivation = _motivation_note(situational_by_team.get(batting_team, {}))
            pitcher_confirmed = getattr(game, "pitchers_confirmed", False)

            for batter_name in rosters.get(batting_team, []):
                considered += 1
                batter_profile = stats_provider.get_batter_profile(batter_name, batting_team)
                score, reasoning, quality = _score_candidate(
                    batter_name, batter_profile, pitcher_profile, hr_park_factor, motivation
                )
                if score is None:
                    if quality == "below_hr_floor":
                        dropped["below_hr_floor"].append(
                            f"{batter_name} ({batting_team}): {batter_profile.hr_count} HR < floor {config.HR_PROP_MIN_SEASON_HR}")
                    else:
                        dropped["no_data"].append(
                            f"{batter_name} ({batting_team}): barrel_pct={batter_profile.barrel_pct}, quality={quality}")
                    continue

                public_lean = public_prop_splits.get((batting_team, batter_name)) if public_prop_splits else None
                if public_lean is not None and public_lean >= 80:
                    if score < 90:
                        dropped["public_fade"].append(f"{batter_name} ({batting_team}): {public_lean:.0f}% public, score {score}")
                        continue
                    reasoning.append(f"Public is {public_lean:.0f}% on the OVER -- kept only because every other signal is elite.")

                if score < config.HR_PROP_STRONG_SCORE:
                    reasoning.append(
                        f"Below our normal high-confidence bar ({config.HR_PROP_STRONG_SCORE}+) -- "
                        f"thinner signal day, shown as the best available rather than a slam-dunk.")

                lineup_flag = lineup_source.get(batting_team, "confirmed")
                if lineup_flag == "roster":
                    reasoning.append(
                        "NOTE: starting lineup not posted yet -- confirm this player is actually "
                        "in today's lineup before betting.")
                if not pitcher_confirmed:
                    reasoning.append(
                        f"NOTE: opposing starter ({opp_pitcher.name}) is MLB's listed probable but NOT yet "
                        f"confirmed for this game -- double-check before betting, and re-run closer to first pitch.")

                candidates.append({
                    "player_name": batter_name,
                    "team": batting_team,
                    "game_id": game.game_id,
                    "opponent_pitcher": opp_pitcher.name,
                    "score": score,
                    "model_prob": score_to_probability(score),
                    "reasoning": reasoning,
                    "data_quality": "roster_unconfirmed" if lineup_flag == "roster" else quality,
                })

    candidates.sort(key=lambda c: c["score"], reverse=True)

    logger.info("HR-DIAG: %d batters considered across %d games; %d scored into the pool.",
                considered, len(games), len(candidates))
    if dropped["no_data"]:
        logger.info("HR-DIAG: dropped %d for MISSING/BAD STATCAST DATA:", len(dropped["no_data"]))
        for d in dropped["no_data"]:
            logger.info("HR-DIAG:    - %s", d)
    if dropped["below_hr_floor"]:
        logger.info("HR-DIAG: dropped %d BELOW SEASON-HR FLOOR:", len(dropped["below_hr_floor"]))
        for d in dropped["below_hr_floor"]:
            logger.info("HR-DIAG:    - %s", d)
    if dropped["public_fade"]:
        logger.info("HR-DIAG: dropped %d for HEAVY PUBLIC FADE:", len(dropped["public_fade"]))
        for d in dropped["public_fade"]:
            logger.info("HR-DIAG:    - %s", d)

    return candidates


def finalize_hr_props(pool, max_per_day=None, recent_miss_players=None):
    """Apply +EV filter, cold-streak cooldown, and one-pick-per-game
    diversification, then take the top N.

    recent_miss_players: set of NORMALIZED names (via _norm_hr) that were picked
      2+ times in the last 7 days and didn't homer -- faded to the bottom.
    Slate is never empty (per your rule)."""
    max_per_day = max_per_day or config.HR_PROP_MAX_PER_DAY
    cold = recent_miss_players or set()

    plus_ev, fallback = [], []
    for c in pool:
        odds = c.get("odds_american")
        if odds is None:
            c["ev_edge"] = None
            c["reasoning"].append(
                "[+EV] HR odds unavailable from the book right now -- shown on model score alone; "
                "confirm the price is fair before betting.")
            fallback.append(c)
            continue
        implied = american_to_implied(odds)
        edge = c["model_prob"] - implied
        c["ev_edge"] = edge
        c["implied_prob"] = implied
        if edge >= config.HR_MIN_EV_EDGE:
            c["reasoning"].append(
                f"[+EV +{edge*100:.1f}%] Our model gives {c['player_name']} a {c['model_prob']*100:.1f}% HR chance "
                f"vs the book's implied {implied*100:.1f}% at {odds:+d} -- real betting value, clears the "
                f"{config.HR_MIN_EV_EDGE*100:.0f}% edge bar.")
            plus_ev.append(c)
        else:
            c["reasoning"].append(
                f"[No edge {edge*100:+.1f}%] Model {c['model_prob']*100:.1f}% vs implied {implied*100:.1f}% "
                f"at {odds:+d} -- not enough value to clear the {config.HR_MIN_EV_EDGE*100:.0f}% bar.")
            fallback.append(c)

    plus_ev.sort(key=lambda c: c["ev_edge"], reverse=True)
    fallback.sort(key=lambda c: c["score"], reverse=True)
    ordered = plus_ev + fallback  # best board order before cooldown/diversification

    # Split cold (recent chronic missers) from fresh, preserving order.
    fresh = [c for c in ordered if _norm_hr(c["player_name"]) not in cold]
    cold_list = [c for c in ordered if _norm_hr(c["player_name"]) in cold]
    for c in cold_list:
        c["reasoning"].append(
            "[Cooldown] Faded -- this bat was picked 2+ times in the last week and didn't homer. "
            "Only shown if the fresh board can't fill the slate.")

    # Select with one-pick-per-game diversification: fresh first, then cold.
    final, used_games, deferred_same_game = [], set(), []
    for c in fresh:
        if c["game_id"] in used_games:
            deferred_same_game.append(c)
            continue
        final.append(c)
        used_games.add(c["game_id"])
        if len(final) >= max_per_day:
            break
    # Fill remaining: relax the one-per-game rule on fresh, then use cold.
    if len(final) < max_per_day:
        for c in deferred_same_game + cold_list:
            if c in final:
                continue
            final.append(c)
            if len(final) >= max_per_day:
                break

    logger.info("HR-DIAG: EV %d+/%d fallback | cold-faded %d | one-per-game diversification -> final %d.",
                len(plus_ev), len(fallback), len(cold_list), len(final))
    logger.info("HR-DIAG: FINAL PICKS: %s",
                ", ".join(f"{c['player_name']} ({c['team']}) score {c['score']:.0f}"
                          + (f" +EV {c['ev_edge']*100:+.1f}%" if c.get('ev_edge') is not None else " (no odds)")
                          for c in final) or "(none)")
    return final


def _score_candidate(batter_name, batter, pitcher, hr_park_factor, motivation_note):
    if batter.data_quality in ("degraded", "not_found") or batter.barrel_pct is None:
        return None, None, batter.data_quality

    if batter.hr_count is not None and batter.hr_count < config.HR_PROP_MIN_SEASON_HR:
        return None, None, "below_hr_floor"

    score = 50.0
    reasoning = []
    reasoning.append("Starting score: 50 (baseline). The SPOT (recent form, matchup, park) drives this more than the name.")

    if batter.hr_count is not None:
        if batter.hr_count >= 30:
            score += 15
            reasoning.append(f"[Season Power +15] {batter_name} has {batter.hr_count} HR -- elite volume (30+). "
                             f"A plus, but the matchup/park below matter more for TODAY.")
        elif batter.hr_count >= 22:
            score += 11
            reasoning.append(f"[Season Power +11] {batter_name} has {batter.hr_count} HR -- a real middle-order slugger (22+).")
        elif batter.hr_count >= 15:
            score += 7
            reasoning.append(f"[Season Power +7] {batter_name} has {batter.hr_count} HR -- solid, legit power (15+).")
        else:
            score += 3
            reasoning.append(f"[Season Power +3] {batter_name} has {batter.hr_count} HR -- modest volume, but cleared the "
                             f"{config.HR_PROP_MIN_SEASON_HR}-HR floor; the value is in the spot, not the name.")
    else:
        reasoning.append("[Season Power n/a] Season HR total unavailable -- scored on contact quality alone.")

    if batter.barrel_pct >= 12:
        score += 16
        reasoning.append(f"[Barrel Signal +16] {batter_name} has an ELITE barrel rate of {batter.barrel_pct:.1f}% "
                         f"(12%+ -- how often he squares a ball up for max damage; the top HR predictor).")
    elif batter.barrel_pct >= 9:
        score += 8
        reasoning.append(f"[Barrel Signal +8] {batter_name} barrels {batter.barrel_pct:.1f}% -- above average power contact.")
    elif batter.barrel_pct < 6:
        score -= 12
        reasoning.append(f"[Barrel Signal -12] {batter_name}'s barrel rate is only {batter.barrel_pct:.1f}% "
                         f"(under 6% -- weak power contact, real drag).")
    else:
        reasoning.append(f"[Barrel Signal +0] {batter_name}'s barrel rate is {batter.barrel_pct:.1f}% (average, neutral).")
    if batter.recent_barrel_trend and batter.recent_barrel_trend > 2:
        score += 12
        reasoning.append(f"[Hot Streak +12] Trending UP: barrel% is +{batter.recent_barrel_trend:.1f} points over the "
                         f"last 15 days -- he's heating up right now, prime value-bat signal.")

    if pitcher and pitcher.barrel_pct_allowed is not None:
        if pitcher.barrel_pct_allowed >= 9:
            score += 16
            reasoning.append(f"[Pitcher Vulnerability +16] Opposing SP {pitcher.name} allows a HIGH barrel rate of "
                             f"{pitcher.barrel_pct_allowed:.1f}% (9%+ -- gives up hard, square contact often).")
        elif pitcher.barrel_pct_allowed >= 7:
            score += 8
            reasoning.append(f"[Pitcher Vulnerability +8] {pitcher.name} allows {pitcher.barrel_pct_allowed:.1f}% "
                             f"barrels -- a bit hittable.")
        elif pitcher.barrel_pct_allowed < 5:
            score -= 12
            reasoning.append(f"[Pitcher Vulnerability -12] {pitcher.name} only allows {pitcher.barrel_pct_allowed:.1f}% "
                             f"barrels (under 5% -- tough to square up, works against this pick).")
        else:
            reasoning.append(f"[Pitcher Vulnerability +0] {pitcher.name} allows {pitcher.barrel_pct_allowed:.1f}% barrels (average).")
    if pitcher and pitcher.hr_per_9 is not None:
        if pitcher.hr_per_9 >= 1.4:
            score += 10
            reasoning.append(f"[HR Rate Allowed +10] {pitcher.name} is running a {pitcher.hr_per_9:.2f} HR/9 "
                             f"(1.4+ -- he serves up homers at a high clip).")
        elif pitcher.hr_per_9 < 0.9:
            score -= 8
            reasoning.append(f"[HR Rate Allowed -8] {pitcher.name} only allows {pitcher.hr_per_9:.2f} HR/9 "
                             f"(under 0.9 -- stingy with the long ball).")
        else:
            reasoning.append(f"[HR Rate Allowed +0] {pitcher.name} allows {pitcher.hr_per_9:.2f} HR/9 (average).")

    if hr_park_factor >= 108:
        score += 12
        reasoning.append(f"[Park Factor +12] This park's HR factor is {hr_park_factor} (108+ -- a hitter's park that "
                         f"inflates home runs).")
    elif hr_park_factor <= 92:
        score -= 10
        reasoning.append(f"[Park Factor -10] This park's HR factor is {hr_park_factor} (92 or below -- a pitcher's park "
                         f"that suppresses home runs).")
    else:
        reasoning.append(f"[Park Factor +0] This park's HR factor is {hr_park_factor} (roughly neutral for homers).")
    if motivation_note:
        reasoning.append(f"[Overlay] {motivation_note}")

    final = round(max(0, min(100, score)), 1)
    reasoning.append(f"FINAL HR SCORE: {final}/100. Higher = stronger homer spot.")
    return final, reasoning, "ok"


def _motivation_note(situational):
    if situational and situational.get("park_runs_factor", 100) >= 110:
        return "Hitter-friendly conditions today."
    return None
