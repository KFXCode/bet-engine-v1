"""
engine/hr_props.py
===================
HR Prop Workflow (runs automatically every day alongside moneyline):
  1. Barrel Signal (BONUS)     -- batter's barrel%/recent trend WHEN AVAILABLE
  2. Pitcher Vulnerability     -- opposing SP's barrel%/hard-hit%/HR-9 allowed
  3. Park + Motivation Overlay -- HR park factor + motivation context
  4. Public Lean Filter        -- fade extremely public props unless elite
  5. +EV Edge tag              -- our probability vs FanDuel implied
  6. Cooldown + Diversification-- fade chronic recent-missers, one pick/game
  7. Value-Longshot board      -- best +450-or-longer bats by EV edge

CRITICAL FIX (Aug 14): we NO LONGER drop a batter just because Statcast barrel
data didn't load. Barrel data is scraped from Baseball Savant and frequently
fails from GitHub's servers -- the old code dropped every one of those bats,
which silently threw out most of the longshot pool (the +500-850 hitters that
keep homering). Now barrel is a BONUS when present; a bat is scored on whatever
reliable data exists (season HR total, opposing pitcher HR/9, park). A batter is
only dropped if there's truly nothing to score on, or he's under the season-HR
floor (config.HR_PROP_MIN_SEASON_HR, now 4).

RANKING: core slots ranked by SCORE (stable across re-runs). PLUS up to
config.HR_VALUE_LONGSHOT_SLOTS "Value Longshot" slots -- the best bats priced
>= config.HR_LONGSHOT_MIN_ODDS by EV edge -- surfaced every day. That's the
automated @MLBHR value board, built from FanDuel's own odds.

COOLDOWN: any batter picked 2+ times in the last 7 days who didn't homer is
faded. DIVERSIFICATION: at most one pick per game. MLB-only. Slate never empty.

DIAGNOSTICS: every batter considered is logged (GitHub Actions log, "HR-DIAG").
"""

import logging
import re as _re
import unicodedata as _ud

import config
from data.park_factors import park_factor_for

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
                score, reasoning, quality = _score_candidate(
                    batter_name, batter_profile, pitcher_profile, hr_park_factor, motivation
                )
                if score is None:
                    if quality == "below_hr_floor":
                        dropped["below_hr_floor"].append(
                            f"{batter_name} ({batting_team}): {batter_profile.hr_count} HR < floor {config.HR_PROP_MIN_SEASON_HR}")
                    else:
                        dropped["no_data"].append(
                            f"{batter_name} ({batting_team}): no season HR and no barrel data")
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
    for label, key in (("MISSING ALL DATA", "no_data"),
                       ("BELOW SEASON-HR FLOOR", "below_hr_floor"),
                       ("HEAVY PUBLIC FADE", "public_fade")):
        if dropped[key]:
            logger.info("HR-DIAG: dropped %d (%s):", len(dropped[key]), label)
            for d in dropped[key]:
                logger.info("HR-DIAG:    - %s", d)
    return candidates


def finalize_hr_props(pool, max_per_day=None, recent_miss_players=None):
    """Core slots ranked by SCORE + Value-Longshot slots ranked by EV edge.
    recent_miss_players: normalized names faded for chronic recent misses.
    Slate is never empty."""
    max_per_day = max_per_day or config.HR_PROP_MAX_PER_DAY
    longshot_slots = getattr(config, "HR_VALUE_LONGSHOT_SLOTS", 0)
    longshot_min = getattr(config, "HR_LONGSHOT_MIN_ODDS", 450)
    cold = recent_miss_players or set()

    for c in pool:
        odds = c.get("odds_american")
        if odds is None:
            c["ev_edge"] = None
            c["reasoning"].append(
                "[Value] HR odds unavailable from FanDuel right now -- ranked on model score; "
                "confirm the price before betting.")
            continue
        implied = american_to_implied(odds)
        edge = c["model_prob"] - implied
        c["ev_edge"] = edge
        c["implied_prob"] = implied
        if edge >= config.HR_MIN_EV_EDGE:
            c["reasoning"].append(
                f"[+EV +{edge*100:.1f}%] Model gives {c['player_name']} {c['model_prob']*100:.1f}% "
                f"vs FanDuel implied {implied*100:.1f}% at {odds:+d} -- real betting value.")
        else:
            c["reasoning"].append(
                f"[Fair price {edge*100:+.1f}%] Model {c['model_prob']*100:.1f}% vs implied {implied*100:.1f}% "
                f"at {odds:+d} -- efficient price; a SPOT play, not a value play.")

    ordered = sorted(pool, key=lambda c: c["score"], reverse=True)
    fresh = [c for c in ordered if _norm_hr(c["player_name"]) not in cold]
    cold_list = [c for c in ordered if _norm_hr(c["player_name"]) in cold]
    for c in cold_list:
        c["reasoning"].append(
            "[Cooldown] Faded -- picked 2+ times in the last week without homering. "
            "Only shown if the fresh board can't fill the slate.")

    # --- CORE slots: highest score, one per game ---
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
    if len(final) < max_per_day:
        for c in deferred + cold_list:
            if c in final:
                continue
            c["pick_type"] = "core"
            final.append(c)
            if len(final) >= max_per_day:
                break

    # --- VALUE-LONGSHOT slots: best +450-or-longer bats by EV edge ---
    chosen_names = {_norm_hr(c["player_name"]) for c in final}
    longshots = [
        c for c in fresh
        if c.get("odds_american") is not None
        and c["odds_american"] >= longshot_min
        and _norm_hr(c["player_name"]) not in chosen_names
        and c.get("ev_edge") is not None
    ]
    longshots.sort(key=lambda c: (c["ev_edge"], c["score"]), reverse=True)
    added = 0
    for c in longshots:
        if c["game_id"] in used_games:
            continue
        c["pick_type"] = "longshot"
        c["reasoning"].insert(
            0, f"[VALUE LONGSHOT] {c['player_name']} at {c['odds_american']:+d} -- a plus-money "
               f"bat our model rates as underpriced. Higher risk, big payout; this is the "
               f"mispriced-book play, not a safe pick.")
        final.append(c)
        used_games.add(c["game_id"])
        added += 1
        if added >= longshot_slots:
            break

    logger.info("HR-DIAG: core %d | longshots %d | cold-faded %d -> final %d.",
                len(final) - added, added, len(cold_list), len(final))
    logger.info("HR-DIAG: FINAL: %s",
                ", ".join(f"{c['player_name']}({c['team']}) {c.get('pick_type','core')} sc{c['score']:.0f}"
                          + (f" EV{c['ev_edge']*100:+.1f}%" if c.get('ev_edge') is not None else " noodds")
                          for c in final) or "(none)")
    return final


def _score_candidate(batter_name, batter, pitcher, hr_park_factor, motivation_note):
    # Drop only if the player truly can't be identified (not_found) OR there is
    # NOTHING to score on. Missing barrel data alone no longer drops a bat.
    if batter.data_quality == "not_found" and batter.hr_count is None:
        return None, None, "not_found"
    if batter.hr_count is None and batter.barrel_pct is None:
        return None, None, "no_data"

    if batter.hr_count is not None and batter.hr_count < config.HR_PROP_MIN_SEASON_HR:
        return None, None, "below_hr_floor"

    score = 50.0
    reasoning = ["Starting score: 50 (baseline). The SPOT (matchup, park, form) drives this more than the name."]

    if batter.hr_count is not None:
        if batter.hr_count >= 30:
            score += 15
            reasoning.append(f"[Season Power +15] {batter_name} has {batter.hr_count} HR -- elite volume (30+).")
        elif batter.hr_count >= 22:
            score += 11
            reasoning.append(f"[Season Power +11] {batter_name} has {batter.hr_count} HR -- middle-order slugger (22+).")
        elif batter.hr_count >= 15:
            score += 7
            reasoning.append(f"[Season Power +7] {batter_name} has {batter.hr_count} HR -- solid legit power (15+).")
        elif batter.hr_count >= 8:
            score += 5
            reasoning.append(f"[Season Power +5] {batter_name} has {batter.hr_count} HR -- real mid-power bat, prime value range.")
        else:
            score += 2
            reasoning.append(f"[Season Power +2] {batter_name} has {batter.hr_count} HR -- modest, cleared the "
                             f"{config.HR_PROP_MIN_SEASON_HR}-HR floor; the value is the spot, not the name.")
    else:
        reasoning.append("[Season Power n/a] Season HR total unavailable -- scored on matchup, park, and barrel.")

    # Barrel is a BONUS layer now -- only applied when the data actually loaded.
    if batter.barrel_pct is not None:
        if batter.barrel_pct >= 12:
            score += 16
            reasoning.append(f"[Barrel Signal +16] ELITE barrel rate {batter.barrel_pct:.1f}% (12%+ -- top HR predictor).")
        elif batter.barrel_pct >= 9:
            score += 8
            reasoning.append(f"[Barrel Signal +8] Barrels {batter.barrel_pct:.1f}% -- above-average power contact.")
        elif batter.barrel_pct < 6:
            score -= 10
            reasoning.append(f"[Barrel Signal -10] Only {batter.barrel_pct:.1f}% barrels (weak power contact).")
        else:
            reasoning.append(f"[Barrel Signal +0] Barrel rate {batter.barrel_pct:.1f}% (average).")
        if batter.recent_barrel_trend and batter.recent_barrel_trend > 2:
            score += 12
            reasoning.append(f"[Hot Streak +12] Barrel% +{batter.recent_barrel_trend:.1f} pts over last 15 days -- heating up.")
    else:
        reasoning.append("[Barrel Signal n/a] Statcast barrel data didn't load today -- scored on the other signals "
                         "(this no longer drops the pick).")

    if pitcher and pitcher.barrel_pct_allowed is not None:
        if pitcher.barrel_pct_allowed >= 9:
            score += 16
            reasoning.append(f"[Pitcher Vulnerability +16] {pitcher.name} allows HIGH barrels {pitcher.barrel_pct_allowed:.1f}% (9%+).")
        elif pitcher.barrel_pct_allowed >= 7:
            score += 8
            reasoning.append(f"[Pitcher Vulnerability +8] {pitcher.name} allows {pitcher.barrel_pct_allowed:.1f}% barrels -- hittable.")
        elif pitcher.barrel_pct_allowed < 5:
            score -= 10
            reasoning.append(f"[Pitcher Vulnerability -10] {pitcher.name} allows only {pitcher.barrel_pct_allowed:.1f}% barrels (tough).")
        else:
            reasoning.append(f"[Pitcher Vulnerability +0] {pitcher.name} allows {pitcher.barrel_pct_allowed:.1f}% barrels (average).")
    if pitcher and pitcher.hr_per_9 is not None:
        if pitcher.hr_per_9 >= 1.4:
            score += 12
            reasoning.append(f"[HR Rate Allowed +12] {pitcher.name} runs {pitcher.hr_per_9:.2f} HR/9 (1.4+ -- serves up homers).")
        elif pitcher.hr_per_9 < 0.9:
            score -= 8
            reasoning.append(f"[HR Rate Allowed -8] {pitcher.name} allows {pitcher.hr_per_9:.2f} HR/9 (under 0.9 -- stingy).")
        else:
            reasoning.append(f"[HR Rate Allowed +0] {pitcher.name} allows {pitcher.hr_per_9:.2f} HR/9 (average).")

    if hr_park_factor >= 108:
        score += 12
        reasoning.append(f"[Park Factor +12] Park HR factor {hr_park_factor} (108+ -- hitter's park).")
    elif hr_park_factor <= 92:
        score -= 10
        reasoning.append(f"[Park Factor -10] Park HR factor {hr_park_factor} (92 or below -- pitcher's park).")
    else:
        reasoning.append(f"[Park Factor +0] Park HR factor {hr_park_factor} (neutral).")
    if motivation_note:
        reasoning.append(f"[Overlay] {motivation_note}")

    final = round(max(0, min(100, score)), 1)
    reasoning.append(f"FINAL HR SCORE: {final}/100. Higher = stronger homer spot.")
    return final, reasoning, "ok"


def _motivation_note(situational):
    if situational and situational.get("park_runs_factor", 100) >= 110:
        return "Hitter-friendly conditions today."
    return None
