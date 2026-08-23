"""
data/public_betting_provider.py
================================
Ticket %/handle % splits. This is the one input in the whole system with NO
free, legal, programmatic source -- sportsbettingdime.com, Action Network,
etc. are websites for humans to read, not APIs. Four modes
(config.PUBLIC_BETTING_MODE):

- "manual" (default): you read today's split off a site like
  sportsbettingdime.com yourself and drop the numbers into
  manual_inputs/public_betting_<date>.json. run_daily.py auto-creates that
  file with neutral 50/50 placeholders + every game listed, so you're just
  editing numbers, not writing JSON from scratch.
- "url": set config.PUBLIC_BETTING_URL once to a splits page and every run
  fetches + parses THAT page fresh -- see data/public_betting_scraper.py.
- "mock": synthetic split, for demoing the pipeline only.
- "api": stub for you to wire up a paid data feed you have access to.

TEMPLATE MERGE (Aug 22, 2026): ensure_manual_template used to bail out
entirely if today's file already existed, so a sport that came online LATER
in the day never got keys. It now MERGES -- missing games are appended at
neutral 50/50, and numbers you already typed are never touched.

LABEL REFRESH (Aug 23, 2026): the "_matchup" label is now also kept in sync
on every run. It used to be written once at key creation and never updated,
so after a team-mapping fix (Portland Fire was normalizing to "LA") the file
still displayed the OLD wrong matchup forever, even though the split itself
was applying correctly (splits key off game_id, not the label). Labels are
cosmetic, but a stale one makes the file impossible to read confidently.
"""

import json
import logging
import random
from dataclasses import dataclass

import config

logger = logging.getLogger(__name__)


@dataclass
class PublicSplit:
    tickets_pct_home: float   # 0..100, % of TICKETS (bet count) on the home team
    handle_pct_home: float    # 0..100, % of HANDLE ($) on the home team
    source: str
    data_quality: str         # "manual" | "mock" | "api" | "missing" | "url" | "partial"


class PublicBettingProvider:
    def get_splits(self, games, date_str):
        """Returns dict game_id -> PublicSplit."""
        raise NotImplementedError


class ManualPublicBettingProvider(PublicBettingProvider):
    def get_splits(self, games, date_str):
        ensure_manual_template(games, date_str)
        path = config.MANUAL_INPUTS_DIR / f"public_betting_{date_str}.json"
        try:
            with open(path) as f:
                raw = json.load(f)
        except Exception as exc:
            logger.warning("Couldn't read %s (%s) -- treating all games as 50/50.", path, exc)
            raw = {}

        out = {}
        for game in games:
            entry = raw.get(game.game_id)
            if entry is None:
                out[game.game_id] = PublicSplit(50.0, 50.0, "manual", "missing")
            else:
                out[game.game_id] = PublicSplit(
                    tickets_pct_home=float(entry.get("tickets_pct_home", 50.0)),
                    handle_pct_home=float(entry.get("handle_pct_home", 50.0)),
                    source=entry.get("source", "manual"),
                    data_quality="manual",
                )
        return out


class MockPublicBettingProvider(PublicBettingProvider):
    def get_splits(self, games, date_str):
        out = {}
        for game in games:
            rng = random.Random(f"{game.game_id}-{date_str}-public")
            tickets = round(rng.uniform(30, 70), 1)
            # handle usually diverges a bit from tickets -- that gap IS the "sharp" signal
            handle = round(max(5, min(95, tickets + rng.uniform(-20, 20))), 1)
            out[game.game_id] = PublicSplit(tickets, handle, "mock", "mock")
        return out


class ApiPublicBettingProvider(PublicBettingProvider):
    """BYO paid feed. Wire your provider's request/parse logic in here -- the
    rest of the engine only ever talks to the PublicSplit dataclass above, so
    nothing else needs to change once you do."""

    def get_splits(self, games, date_str):
        raise NotImplementedError(
            "Plug in your paid public-betting-splits feed here (see docstring)."
        )


class UrlPublicBettingProvider(PublicBettingProvider):
    """Fetches + parses config.PUBLIC_BETTING_URL fresh every run instead of
    reading manual_inputs/*.json -- see data/public_betting_scraper.py for
    the actual fetch/parse logic and its known limitations."""

    def get_splits(self, games, date_str):
        from data.public_betting_scraper import fetch_and_parse_splits

        if not config.PUBLIC_BETTING_URL:
            logger.warning("PUBLIC_BETTING_MODE=url but PUBLIC_BETTING_URL isn't set -- treating all games as 50/50.")
            return {g.game_id: PublicSplit(50.0, 50.0, "url", "missing") for g in games}

        found = fetch_and_parse_splits(config.PUBLIC_BETTING_URL, games)
        out = {}
        for game in games:
            out[game.game_id] = found.get(
                game.game_id, PublicSplit(50.0, 50.0, config.PUBLIC_BETTING_URL, "missing")
            )
        return out


def ensure_manual_template(games, date_str):
    """Make sure today's manual file lists EVERY game on today's slate, with a
    correct matchup label.

    - Creates the file if missing.
    - MERGES in any game not in it yet, at neutral 50/50.
    - REFRESHES the "_matchup" label on games already in it.

    Percentages you've typed are never modified -- this only ever adds keys and
    corrects labels. That's what lets a sport coming online later in the day
    (NFL preseason, any season opener) still be fillable, and keeps labels
    honest after a team-normalization fix."""
    path = config.MANUAL_INPUTS_DIR / f"public_betting_{date_str}.json"
    config.MANUAL_INPUTS_DIR.mkdir(exist_ok=True)

    existing = {}
    if path.exists():
        try:
            with open(path) as f:
                existing = json.load(f) or {}
        except Exception as exc:
            logger.warning("Couldn't parse %s (%s) -- rewriting it as a fresh template.", path, exc)
            existing = {}

    added = []
    relabeled = []
    for game in games:
        label = f"{game.away_team} @ {game.home_team}"
        entry = existing.get(game.game_id)
        if entry is None:
            existing[game.game_id] = {
                "_matchup": label,
                "tickets_pct_home": 50.0,
                "handle_pct_home": 50.0,
                "source": "sportsbettingdime.com",
            }
            added.append(f"{label} ({game.sport})")
        elif entry.get("_matchup") != label:
            # Label only -- the typed percentages are left exactly as they are.
            old = entry.get("_matchup")
            entry["_matchup"] = label
            relabeled.append(f"{old} -> {label}")

    if not added and not relabeled:
        return

    with open(path, "w") as f:
        json.dump(existing, f, indent=2)
    if added:
        logger.info("Public-betting template %s: added %d game(s) -- %s. Fill in real tickets/handle %% "
                    "before you trust those edges.", path, len(added), ", ".join(added[:12]))
    if relabeled:
        logger.info("Public-betting template %s: corrected %d matchup label(s) -- %s",
                    path, len(relabeled), ", ".join(relabeled[:12]))


def get_public_betting_provider():
    if config.PUBLIC_BETTING_MODE == "mock":
        return MockPublicBettingProvider()
    if config.PUBLIC_BETTING_MODE == "api":
        return ApiPublicBettingProvider()
    if config.PUBLIC_BETTING_MODE == "url":
        return UrlPublicBettingProvider()
    return ManualPublicBettingProvider()
