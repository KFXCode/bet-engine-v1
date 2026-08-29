"""
engine/hr_props.py
===================
HR Prop Workflow (runs automatically every day alongside moneyline).

ONE ranked board: a single list ranked by SCORE -- the model's read of who is
most likely to homer today. Plus-money bats compete in the same list; there is
no separate "longshot" tier. Scoring is spot-driven (recent barrel form,
opposing pitcher HR/9 and barrels allowed, park) with season HR total as a
supporting factor, so a hot mid-power bat at +500 in a great matchup can rank
above a cold chalk slugger.

PERFORMANCE BENCH (Aug 29, 2026): the board now consults
data/prop_performance.get_chronic_missers() and drops bats the results have
already disproven. The short rotation fade wasn't enough -- it only looked
back 3-7 days, so the same names cycled back in forever. The ledger showed
Ben Rice 14 picks / 1 hit, Kazuma Okamoto 13 / 0 and Pete Alonso 12 / 0,
together 29% of every HR pick ever made and a combined 1-for-39 across 47
different players used. That is a model failure, not variance, and it needs
long memory to fix: chronic missers are benched for weeks, not days.

  1. Barrel Signal (BONUS)     -- batter's barrel%/recent trend WHEN AVAILABLE
  2. Pitcher Vulnerability     -- opposing SP's barrel%/hard-hit%/HR-9 allowed
  3. Park + Motivation Overlay -- HR park factor + motivation context
  4. Public Lean Filter        -- fade extremely public props unless elite
  5. +EV Edge tag              -- our probability vs the book's implied (a TAG)
  6. Rotation + Performance    -- fade recent repeats AND chronic missers

Missing Statcast barrel data no longer drops a bat (barrel is a bonus layer).

DIAGNOSTICS: every batter considered is logged (GitHub Actions log, "HR-DIAG").
"""

import logging
import re as _re
import unicodedata as _ud

import config
from data.park_factors import park_factor_for
from data.recent_form import get_recent_form

logger = logging.getLogger("hr_props")


def _norm_hr(name):
    if not name:
        return ""
    n = _ud.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    n = _re.sub(r"[.\,']", "", n)
    n = _re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", n)
    return _re.sub(r"\s+", " ", n).strip()


def score_to_probability(score):
    p = config.HR_PROB_BASE + (score - 50) * config.HR_PROB_PER_POINT
    return max(config.HR_PROB_MIN, min(config.HR_PROB_MAX, p))


def american_to_implied(ml):
    ml = float(ml)
    return 100.0 / (ml + 100.0) if ml > 0 else -ml / (-ml + 100.0)


def evaluate_hr_prop_candidates(games, rosters, stats_provider, public_prop_splits,
                                 situational_by_team, lineup_source=None):
    """Returns the FULL scored candidate pool (sorted by score, not truncated)."""
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
                form = _safe_recent_form(batter_profile)
                score, reasoning, quality = _score_candidate(
                    batter_name, batter_profile, pitcher_profile, hr_park_factor, motivation, form
                )
                if score is None:
                    if quality == "below_hr_floor":
                        dropped["below_hr_floor"].append(
                            f"{batter_name} ({batting_team}): {batter_profile.hr_count} HR < floor {config.HR_PROP_MIN_SEASON_HR}, no recent form")
                    else:
                        dropped["no_data"].append(f"{batter_name} ({batting_team}): nothing to score on")
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
                        f"confirmed -- double-check before betting, and re-run closer to first pitch.")

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
    for label, key in (("MISSING ALL DATA", "no_data"),
                       ("BELOW SEASON-HR FLOOR", "below_hr_floor"),
                       ("HEAVY PUBLIC FADE", "public_fade")):
        if dropped[key]:
            logger.info("HR-DIAG: dropped %d (%s):", len(dropped[key]), label)
            for d in dropped[key]:
                logger.info("HR-DIAG:    - %s", d)
    if candidates:
        logger.info("HR-DIAG: score spread -- top %.1f / median %.1f / low %.1f",
                    candidates[0]["score"], candidates[len(candidates) // 2]["score"],
                    candidates[-1]["score"])
    return candidates


def _safe_recent_form(batter_profile):
    """Recent form by resolved player_id. None on any miss -- never a drop."""
    pid = getattr(batter_profile, "player_id", None)
    try:
        return get_recent_form(pid) if pid else None
    except Exception as exc:
        logger.debug("recent-form lookup failed for %s: %s", getattr(batter_profile, "name", "?"), exc)
        return None


def _chronic_missers():
    """Long-memory bench list. Empty set on any failure -- never blocks a board."""
    try:
        from data.prop_performance import get_chronic_missers
        return get_chronic_missers("hr_prop")
    except Exception as exc:
        logger.warning("Performance bench unavailable (%s) -- rotation fade only.", exc)
        return set()


def finalize_hr_props(pool, max_per_day=None, recent_miss_players=None):
    """ONE board ranked by SCORE, with two independent fades:
       - ROTATION  : picked in the last few days (short memory, passed in)
       - PERFORMANCE: chronic missers over weeks (long memory, from results)
    Then one pick per game, capped at max_per_day. +EV is a TAG only.
    The slate is never empty -- if every bat is faded we still fill it, but
    the reasoning says exactly why each was flagged."""
    max_per_day = max_per_day or config.HR_PROP_MAX_PER_DAY
    rotation_cold = set(recent_miss_players or set())
    perf_cold = _chronic_missers()

    for c in pool:
        odds = c.get("odds_american")
        if odds is None:
            c["ev_edge"] = None
            c["reasoning"].append(
                "[Value] HR odds unavailable right now -- ranked on model score; confirm the price before betting.")
            continue
        implied = american_to_implied(odds)
        edge = c["model_prob"] - implied
        c["ev_edge"] = edge
        c["implied_prob"] = implied
        if edge >= config.HR_MIN_EV_EDGE:
            c["reasoning"].append(
                f"[+EV +{edge*100:.1f}%] Model {c['model_prob']*100:.1f}% vs book implied {implied*100:.1f}% at {odds:+d} -- real value.")
        else:
            c["reasoning"].append(
                f"[Fair price {edge*100:+.1f}%] Model {c['model_prob']*100:.1f}% vs implied {implied*100:.1f}% at {odds:+d} -- a SPOT play.")

    ordered = sorted(pool, key=lambda c: c["score"], reverse=True)

    fresh, benched, recent = [], [], []
    for c in ordered:
        key = _norm_hr(c["player_name"])
        if key in perf_cold:
            c["reasoning"].append(
                "[Performance bench] This system has picked him repeatedly and he has not "
                "delivered -- benched for weeks so the board stops recycling names the "
                "results have already disproven.")
            benched.append(c)
        elif key in rotation_cold:
            c["reasoning"].append(
                "[Cooldown] Picked in the last few days without homering -- rotated out to "
                "keep the board fresh.")
            recent.append(c)
        else:
            fresh.append(c)

    final, used_games, deferred = [], set(), []
    for c in fresh:
        if c["game_id"] in used_games:
            deferred.append(c)
            continue
        c["pick_type"] = "core"
        final.append(c)
        used_games.add(c["game_id"])
        if len(final) >= max_per_day:
            break

    # Backfill order: another bat from a used game, then short-cooldown names,
    # and only as a last resort a chronic misser.
    if len(final) < max_per_day:
        for c in deferred + recent + benched:
            if c in final:
                continue
            c["pick_type"] = "core"
            final.append(c)
            if len(final) >= max_per_day:
                break

    logger.info("HR-DIAG: pool %d | rotation-faded %d | performance-benched %d | final %d (cap %d).",
                len(pool), len(recent), len(benched), len(final), max_per_day)
    logger.info("HR-DIAG: FINAL: %s",
                ", ".join(f"{c['player_name']}({c['team']}) sc{c['score']:.0f}"
                          + (f" EV{c['ev_edge']*100:+.1f}%" if c.get('ev_edge') is not None else " noodds")
                          for c in final) or "(none)")
    return final


def _score_candidate(batter_name, batter, pitcher, hr_park_factor, motivation_note, form):
    has_form = bool(form and form.get("pa", 0) >= 5)
    if batter.data_quality == "not_found" and batter.hr_count is None and not has_form:
        return None, None, "not_found"
    if batter.hr_count is None and batter.barrel_pct is None and not has_form:
        return None, None, "no_data"
    if (batter.hr_count is not None and batter.hr_count < config.HR_PROP_MIN_SEASON_HR
            and not (has_form and form.get("hot_score", 0) >= 0.5)):
        return None, None, "below_hr_floor"

    score = 35.0
    reasoning = ["Base 35. Scale is built so 100 is the absolute ceiling -- a 55-70 is a decent "
                 "spot, 75-90 is genuinely strong. Recent form is the heaviest input."]

    # 1. RECENT FORM (up to +22) -- reliable, the driver.
    if has_form:
        hot = form["hot_score"]
        bump = round(22 * hot, 1)
        score += bump
        reasoning.append(
            f"[Recent Form +{bump}/22] Last {form['games']} G: {form['hr']} HR, ISO {form['iso']:.3f}, "
            f"{form['hr_rate']*100:.1f}% HR/PA -> hot_score {hot:.2f}.")
        if hot < 0.15 and form["pa"] >= 25:
            score -= 8
            reasoning.append(f"[Cold -8] Ice-cold over the last {form['games']} games -- no recent power.")
    else:
        reasoning.append("[Recent Form n/a] No recent game-log data -- scored on season power, matchup, and park.")

    # 2. Season power (up to +8) -- support, not the driver.
    if batter.hr_count is not None:
        if batter.hr_count >= 30:
            score += 8; reasoning.append(f"[Season Power +8/8] {batter.hr_count} HR -- elite volume.")
        elif batter.hr_count >= 22:
            score += 6; reasoning.append(f"[Season Power +6/8] {batter.hr_count} HR -- middle-order slugger.")
        elif batter.hr_count >= 15:
            score += 4; reasoning.append(f"[Season Power +4/8] {batter.hr_count} HR -- legit power.")
        elif batter.hr_count >= 8:
            score += 2; reasoning.append(f"[Season Power +2/8] {batter.hr_count} HR -- mid-power, value range.")
        else:
            score += 1; reasoning.append(f"[Season Power +1/8] {batter.hr_count} HR -- the value is the spot, not the name.")

    # 3. Barrel bonus (up to +10) -- only when Savant loads.
    if batter.barrel_pct is not None:
        if batter.barrel_pct >= 12:
            score += 10; reasoning.append(f"[Barrel +10/10] ELITE {batter.barrel_pct:.1f}% barrels.")
        elif batter.barrel_pct >= 9:
            score += 5; reasoning.append(f"[Barrel +5/10] {batter.barrel_pct:.1f}% barrels -- above avg.")
        elif batter.barrel_pct < 6:
            score -= 6; reasoning.append(f"[Barrel -6] Only {batter.barrel_pct:.1f}% barrels.")

    # 4. Pitcher vulnerability (up to +10) and HR/9 allowed (up to +8).
    if pitcher and pitcher.barrel_pct_allowed is not None:
        if pitcher.barrel_pct_allowed >= 9:
            score += 10; reasoning.append(f"[Pitcher Vuln +10/10] {pitcher.name} allows {pitcher.barrel_pct_allowed:.1f}% barrels.")
        elif pitcher.barrel_pct_allowed >= 7:
            score += 5; reasoning.append(f"[Pitcher Vuln +5/10] {pitcher.name} allows {pitcher.barrel_pct_allowed:.1f}% barrels.")
        elif pitcher.barrel_pct_allowed < 5:
            score -= 8; reasoning.append(f"[Pitcher Vuln -8] {pitcher.name} stingy ({pitcher.barrel_pct_allowed:.1f}%).")
    if pitcher and pitcher.hr_per_9 is not None:
        if pitcher.hr_per_9 >= 1.4:
            score += 8; reasoning.append(f"[HR Rate Allowed +8/8] {pitcher.name} {pitcher.hr_per_9:.2f} HR/9 -- serves them up.")
        elif pitcher.hr_per_9 >= 1.15:
            score += 4; reasoning.append(f"[HR Rate Allowed +4/8] {pitcher.name} {pitcher.hr_per_9:.2f} HR/9 -- homer-prone.")
        elif pitcher.hr_per_9 < 0.9:
            score -= 8; reasoning.append(f"[HR Rate Allowed -8] {pitcher.name} {pitcher.hr_per_9:.2f} HR/9 -- stingy.")

    # 5. Park (up to +7).
    if hr_park_factor >= 112:
        score += 7; reasoning.append(f"[Park +7/7] HR factor {hr_park_factor} -- strong hitter's park.")
    elif hr_park_factor >= 105:
        score += 4; reasoning.append(f"[Park +4/7] HR factor {hr_park_factor} -- hitter-leaning.")
    elif hr_park_factor <= 92:
        score -= 8; reasoning.append(f"[Park -8] HR factor {hr_park_factor} -- pitcher's park.")
    if motivation_note:
        reasoning.append(f"[Overlay] {motivation_note}")

    final = round(max(0, min(100, score)), 1)
    reasoning.append(f"FINAL HR SCORE: {final}/100.")
    return final, reasoning, "ok"


def _motivation_note(situational):
    if situational and situational.get("park_runs_factor", 100) >= 110:
        return "Hitter-friendly conditions today."
    return None
