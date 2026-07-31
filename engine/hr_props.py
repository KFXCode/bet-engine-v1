"""
engine/hr_props.py
===================
HR Prop Workflow (runs automatically every day alongside moneyline):
  1. Barrel Signal Check       -- batter's own barrel%/recent trend
  2. Pitcher Vulnerability     -- opposing SP's barrel%/hard-hit%/HR-9 allowed
  3. Park + Motivation Overlay -- HR park factor + motivation context
  4. Public Lean Filter        -- fade extremely public props unless elite
  5. Final Selection           -- only the strongest signals survive

Composite score is 0-100. HR props are MLB-only.

DIAGNOSTICS: every batter considered is logged with the exact reason it was
kept or dropped (missing Statcast data, below the season-HR floor, below the
pool cut, etc). Read it in the GitHub Actions run log under "HR-DIAG" to see
why a specific player (e.g. Acuna Jr) did or didn't surface -- real data, not
guesswork.
"""

import logging

import config
from data.park_factors import park_factor_for

logger = logging.getLogger("hr_props")


def evaluate_hr_prop_candidates(games, rosters, stats_provider, public_prop_splits,
                                 situational_by_team, lineup_source=None):
    """
    rosters: dict team_abbr -> list[str] batter names (confirmed lineup when
             posted, else active roster -- see lineup_source)
    public_prop_splits: dict (team_abbr, batter_name) -> pct_public_on_over (0-100), optional
    situational_by_team: dict team_abbr -> summary dict
    lineup_source: dict team_abbr -> "confirmed" | "roster"
    """
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
                    "reasoning": reasoning,
                    "data_quality": "roster_unconfirmed" if lineup_flag == "roster" else quality,
                })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    strongest = [c for c in candidates if c["score"] >= config.HR_PROP_MIN_SCORE]
    final = strongest[: config.HR_PROP_MAX_PER_DAY]

    # ---- DIAGNOSTIC DUMP (read in the Actions log) ----
    logger.info("HR-DIAG: %d batters considered across %d games.", considered, len(games))
    logger.info("HR-DIAG: %d scored, %d made the final cut (max %d, min score %s).",
                len(candidates), len(final), config.HR_PROP_MAX_PER_DAY, config.HR_PROP_MIN_SCORE)
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
    scored_but_cut = [c for c in candidates if c not in final]
    if scored_but_cut:
        logger.info("HR-DIAG: %d scored but below the pool cut:", len(scored_but_cut))
        for c in scored_but_cut:
            logger.info("HR-DIAG:    - %s (%s): scored %.1f", c["player_name"], c["team"], c["score"])
    logger.info("HR-DIAG: FINAL PICKS: %s",
                ", ".join(f"{c['player_name']} {c['score']:.1f}" for c in final) or "(none)")

    return final


def _score_candidate(batter_name, batter, pitcher, hr_park_factor, motivation_note):
    if batter.data_quality in ("degraded", "not_found") or batter.barrel_pct is None:
        return None, None, batter.data_quality

    if batter.hr_count is not None and batter.hr_count < config.HR_PROP_MIN_SEASON_HR:
        return None, None, "below_hr_floor"

    score = 50.0
    reasoning = []
    reasoning.append(f"Starting score: 50 (baseline). Every factor below adds or subtracts points.")

    if batter.hr_count is not None:
        if batter.hr_count >= 30:
            score += 25
            reasoning.append(f"[Season Power +25] {batter_name} has {batter.hr_count} HR this season -- ELITE "
                             f"(30+, one of the league's premier power bats). This is the biggest single factor.")
        elif batter.hr_count >= 22:
            score += 18
            reasoning.append(f"[Season Power +18] {batter_name} has {batter.hr_count} HR this season -- a genuine "
                             f"middle-of-the-order slugger (22+).")
        elif batter.hr_count >= 15:
            score += 10
            reasoning.append(f"[Season Power +10] {batter_name} has {batter.hr_count} HR this season -- solid, "
                             f"legit power (15+).")
        else:
            reasoning.append(f"[Season Power +0] {batter_name} has {batter.hr_count} HR this season -- cleared the "
                             f"{config.HR_PROP_MIN_SEASON_HR}-HR eligibility floor but not a big-power bat.")
    else:
        reasoning.append("[Season Power n/a] Season HR total unavailable today -- scored on contact quality alone.")

    if batter.barrel_pct >= 12:
        score += 12
        reasoning.append(f"[Barrel Signal +12] {batter_name} has an ELITE barrel rate of {batter.barrel_pct:.1f}% "
                         f"(12%+ is top-tier power contact -- how often he squares a ball up for max damage).")
    elif batter.barrel_pct < 6:
        score -= 10
        reasoning.append(f"[Barrel Signal -10] {batter_name}'s barrel rate is only {batter.barrel_pct:.1f}% "
                         f"(under 6% -- weak power contact, drags this down).")
    else:
        reasoning.append(f"[Barrel Signal +0] {batter_name}'s barrel rate is {batter.barrel_pct:.1f}% (average range, neutral).")
    if batter.recent_barrel_trend and batter.recent_barrel_trend > 2:
        score += 8
        reasoning.append(f"[Hot Streak +8] Trending UP: barrel% is +{batter.recent_barrel_trend:.1f} points over the "
                         f"last 15 days -- he's heating up right now.")

    if pitcher and pitcher.barrel_pct_allowed is not None:
        if pitcher.barrel_pct_allowed >= 9:
            score += 12
            reasoning.append(f"[Pitcher Vulnerability +12] Opposing SP {pitcher.name} allows a HIGH barrel rate of "
                             f"{pitcher.barrel_pct_allowed:.1f}% (9%+ -- gives up hard, square contact often).")
        elif pitcher.barrel_pct_allowed < 5:
            score -= 10
            reasoning.append(f"[Pitcher Vulnerability -10] {pitcher.name} only allows {pitcher.barrel_pct_allowed:.1f}% "
                             f"barrels (under 5% -- tough to square up, works against this pick).")
        else:
            reasoning.append(f"[Pitcher Vulnerability +0] {pitcher.name} allows {pitcher.barrel_pct_allowed:.1f}% barrels (average).")
    if pitcher and pitcher.hr_per_9 is not None:
        if pitcher.hr_per_9 >= 1.4:
            score += 8
            reasoning.append(f"[HR Rate Allowed +8] {pitcher.name} is running a {pitcher.hr_per_9:.2f} HR/9 "
                             f"(1.4+ -- he serves up homers at a high clip).")
        elif pitcher.hr_per_9 < 0.9:
            score -= 8
            reasoning.append(f"[HR Rate Allowed -8] {pitcher.name} only allows {pitcher.hr_per_9:.2f} HR/9 "
                             f"(under 0.9 -- stingy with the long ball).")
        else:
            reasoning.append(f"[HR Rate Allowed +0] {pitcher.name} allows {pitcher.hr_per_9:.2f} HR/9 (average).")

    if hr_park_factor >= 108:
        score += 10
        reasoning.append(f"[Park Factor +10] This park's HR factor is {hr_park_factor} (108+ -- a hitter's park that "
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
    reasoning.append(f"FINAL HR SCORE: {final}/100. Higher = stronger homer spot; today's picks are the highest scores on the slate.")
    return final, reasoning, "ok"


def _motivation_note(situational):
    if situational and situational.get("park_runs_factor", 100) >= 110:
        return "Hitter-friendly conditions today."
    return None
