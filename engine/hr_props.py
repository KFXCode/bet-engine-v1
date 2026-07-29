"""
engine/hr_props.py
===================
HR Prop Workflow (runs automatically every day alongside moneyline):
  0. Power-Hitter Pool         -- restrict candidates to the top-N players
                                   (config.HR_PROP_TOP_N_POOL) by SEASON HR
                                   total across today's whole slate.
  1. Barrel Signal Check       -- batter's own barrel%/recent trend
  2. Pitcher Vulnerability     -- opposing SP's barrel%/hard-hit%/HR-9 allowed
  3. Park + Motivation Overlay -- HR park factor + motivation context
  4. Public Lean Filter        -- fade extremely public props unless elite
  5. Dedupe + Final Selection  -- ONE entry per player (best game on a
                                   doubleheader day), highest scores, capped
                                   at HR_PROP_MAX_PER_DAY.

Composite score is 0-100. On any day with zero MLB games this returns [].
"""

import config
from data.park_factors import park_factor_for


def evaluate_hr_prop_candidates(games, rosters, stats_provider, public_prop_splits,
                                 situational_by_team, lineup_source=None):
    lineup_source = lineup_source or {}

    # --- Step 0: build the eligible power-hitter pool --------------------
    pool = []
    for game in games:
        for batting_team, opp_pitcher in (
            (game.home_team, game.away_pitcher),
            (game.away_team, game.home_pitcher),
        ):
            if not opp_pitcher:
                continue
            for batter_name in rosters.get(batting_team, []):
                batter_profile = stats_provider.get_batter_profile(batter_name, batting_team)
                hr_count = batter_profile.hr_count or 0
                pool.append({
                    "batter_name": batter_name,
                    "batter_profile": batter_profile,
                    "batting_team": batting_team,
                    "opp_pitcher": opp_pitcher,
                    "game": game,
                    "hr_count": hr_count,
                })

    pool = [c for c in pool if c["hr_count"] >= config.HR_PROP_MIN_SEASON_HR]
    pool.sort(key=lambda c: c["hr_count"], reverse=True)
    pool = pool[: config.HR_PROP_TOP_N_POOL]

    # --- Steps 1-4: full scoring, only on that restricted pool -----------
    candidates = []
    for entry in pool:
        game = entry["game"]
        opp_pitcher = entry["opp_pitcher"]
        batting_team = entry["batting_team"]
        batter_name = entry["batter_name"]
        batter_profile = entry["batter_profile"]

        pitcher_profile = stats_provider.get_pitcher_profile(opp_pitcher.name)
        hr_park_factor = park_factor_for(game.home_team)[1]
        motivation = _motivation_note(situational_by_team.get(batting_team, {}))

        score, reasoning, quality = _score_candidate(
            batter_name, batter_profile, pitcher_profile, hr_park_factor,
            motivation, entry["hr_count"]
        )
        if score is None:
            continue

        public_lean = public_prop_splits.get((batting_team, batter_name)) if public_prop_splits else None
        if public_lean is not None and public_lean >= 80:
            if score < 90:
                continue
            reasoning.append(f"Public is {public_lean:.0f}% on the OVER -- kept only because every other signal is elite.")

        if score < config.HR_PROP_STRONG_SCORE:
            reasoning.append(
                f"Below our normal high-confidence bar ({config.HR_PROP_STRONG_SCORE}+) -- "
                f"thinner signal day, shown as the best available rather than a slam-dunk."
            )

        lineup_flag = lineup_source.get(batting_team, "confirmed")
        if lineup_flag == "roster":
            reasoning.append(
                "NOTE: starting lineup not posted yet -- confirm this player is actually "
                "in today's lineup before betting."
            )

        # On a doubleheader, name the EXACT game this HR pick is for, so it's
        # never ambiguous which game to bet (the Sal Stewart bug).
        dh_note = game.dh_reasoning()
        if dh_note:
            reasoning.insert(0, dh_note)

        candidates.append({
            "player_name": batter_name + game.dh_label(),
            "player_key": _player_key(batter_name),
            "team": batting_team,
            "game_id": game.game_id,
            "opponent_pitcher": opp_pitcher.name,
            "score": score,
            "reasoning": reasoning,
            "data_quality": "roster_unconfirmed" if lineup_flag == "roster" else quality,
        })

    # --- Step 5: dedupe to ONE entry per player, keep the best game ------
    # A doubleheader put the same hitter in the pool twice (once per game).
    # We only want a single HR pick per player -- the higher-scoring matchup
    # -- so we never flag both games and can't split a bet across the wrong
    # one. Applies to EVERY player, not any specific name.
    best_by_player = {}
    for c in candidates:
        key = c["player_key"]
        if key not in best_by_player or c["score"] > best_by_player[key]["score"]:
            best_by_player[key] = c
    deduped = list(best_by_player.values())

    deduped.sort(key=lambda c: c["score"], reverse=True)
    strongest = [c for c in deduped if c["score"] >= config.HR_PROP_MIN_SCORE]
    return strongest[: config.HR_PROP_MAX_PER_DAY]


def _player_key(name):
    return name.strip().lower()


def _score_candidate(batter_name, batter, pitcher, hr_park_factor, motivation_note, hr_count=0):
    if batter.data_quality in ("degraded", "not_found") or batter.barrel_pct is None:
        return None, None, batter.data_quality

    score = 50.0
    reasoning = []
    reasoning.append("Starting score: 50 (baseline). Every factor below adds or subtracts points.")
    reasoning.append(f"[Power Pool] {batter_name} has {hr_count} HR this season -- among the top "
                     f"{config.HR_PROP_TOP_N_POOL} sluggers on today's slate, so he's in the eligible pool.")

    if hr_count >= 30:
        score += 8
        reasoning.append(f"[HR Volume +8] {hr_count} HR is elite league-leading power.")
    elif hr_count >= 20:
        score += 5
        reasoning.append(f"[HR Volume +5] {hr_count} HR is strong season-long power.")

    if batter.barrel_pct >= 12:
        score += 12
        reasoning.append(f"[Barrel Signal +12] {batter_name} has an ELITE barrel rate of {batter.barrel_pct:.1f}% "
                         f"(12%+ is top-tier power contact).")
    elif batter.barrel_pct < 6:
        score -= 10
        reasoning.append(f"[Barrel Signal -10] {batter_name}'s barrel rate is only {batter.barrel_pct:.1f}% "
                         f"(under 6% -- weak power contact).")
    else:
        reasoning.append(f"[Barrel Signal +0] {batter_name}'s barrel rate is {batter.barrel_pct:.1f}% (average range).")
    if batter.recent_barrel_trend and batter.recent_barrel_trend > 2:
        score += 8
        reasoning.append(f"[Hot Streak +8] Trending UP: barrel% is +{batter.recent_barrel_trend:.1f} points over the "
                         f"last 15 days.")

    if pitcher and pitcher.barrel_pct_allowed is not None:
        if pitcher.barrel_pct_allowed >= 9:
            score += 12
            reasoning.append(f"[Pitcher Vulnerability +12] Opposing SP {pitcher.name} allows a HIGH barrel rate of "
                             f"{pitcher.barrel_pct_allowed:.1f}%.")
        elif pitcher.barrel_pct_allowed < 5:
            score -= 10
            reasoning.append(f"[Pitcher Vulnerability -10] {pitcher.name} only allows {pitcher.barrel_pct_allowed:.1f}% barrels.")
        else:
            reasoning.append(f"[Pitcher Vulnerability +0] {pitcher.name} allows {pitcher.barrel_pct_allowed:.1f}% barrels (average).")
    if pitcher and pitcher.hr_per_9 is not None:
        if pitcher.hr_per_9 >= 1.4:
            score += 8
            reasoning.append(f"[HR Rate Allowed +8] {pitcher.name} is running a {pitcher.hr_per_9:.2f} HR/9.")
        elif pitcher.hr_per_9 < 0.9:
            score -= 8
            reasoning.append(f"[HR Rate Allowed -8] {pitcher.name} only allows {pitcher.hr_per_9:.2f} HR/9.")
        else:
            reasoning.append(f"[HR Rate Allowed +0] {pitcher.name} allows {pitcher.hr_per_9:.2f} HR/9 (average).")

    if hr_park_factor >= 108:
        score += 10
        reasoning.append(f"[Park Factor +10] This park's HR factor is {hr_park_factor} (hitter's park).")
    elif hr_park_factor <= 92:
        score -= 10
        reasoning.append(f"[Park Factor -10] This park's HR factor is {hr_park_factor} (pitcher's park).")
    else:
        reasoning.append(f"[Park Factor +0] This park's HR factor is {hr_park_factor} (roughly neutral).")
    if motivation_note:
        reasoning.append(f"[Overlay] {motivation_note}")

    final = round(max(0, min(100, score)), 1)
    reasoning.append(f"FINAL HR SCORE: {final}/100. Higher = stronger homer spot.")
    return final, reasoning, "ok"


def _motivation_note(situational):
    if situational and situational.get("park_runs_factor", 100) >= 110:
        return "Hitter-friendly conditions today."
    return None
