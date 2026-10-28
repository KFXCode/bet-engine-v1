"""
tests/test_sharp_price.py
==========================
NBA/NHL picks are judged against the sharp books' fair price (Oct 28, 2026).
Run with:  python -m unittest discover -s tests -t .
"""

import unittest
from datetime import date

import config
from engine.models import Game, MoneylineOdds
from engine.scoring import evaluate_game
from engine import strategy_rules
from data import odds_providers as op


def _event(fd, sharp, sharp_key="pinnacle", fd_seen="2026-10-28T22:00:00Z", sharp_seen="2026-10-28T21:55:00Z"):
    def bm(key, prices, seen):
        return {"key": key, "last_update": seen, "markets": [{"key": "h2h", "outcomes": [
            {"name": "Boston Celtics", "price": prices[0]}, {"name": "Brooklyn Nets", "price": prices[1]}]}]}
    books = [bm("fanduel", fd, fd_seen)]
    if sharp:
        books.append(bm(sharp_key, sharp, sharp_seen))
    return {"home_team": "Boston Celtics", "away_team": "Brooklyn Nets", "bookmakers": books}


GAME = Game("nba-1", "2026-10-28", "BOS", "BKN", "2026-10-28T23:30:00Z", sport="NBA")


def _eval(fd, sharp, **kw):
    odds = op._parse_odds_event(_event(fd, sharp, **kw), "fanduel", GAME, "now", "NBA")
    return odds, evaluate_game(GAME, odds, None, None, None, None, {}, {}, None, {}, run_date=date(2026, 10, 28))


class TestSharpPrice(unittest.TestCase):
    def test_asks_for_sharp_books_for_free(self):
        self.assertEqual(op.TheOddsApiProvider(api_key="x", sport="NBA")._bookmakers(),
                         "fanduel,pinnacle,lowvig,betonlineag")
        self.assertEqual(op.TheOddsApiProvider(api_key="x", sport="MLB")._bookmakers(), "fanduel")

    def test_picks_the_side_fanduel_overpays(self):
        # Pinnacle: Nets +150 / Celtics -170 (Nets ~39%). FanDuel pays Nets +175.
        odds, ev = _eval((-210, 175), (-170, 150))
        self.assertEqual(odds.sharp_source, "Pinnacle")
        self.assertEqual(ev.recommended_side, "away")
        self.assertGreater(ev.sharp_ev, config.SHARP_MIN_EV)
        plays, _ = strategy_rules.select_daily_plays([ev], _FakeDB(), {}, "2026-10-28")
        self.assertEqual(len(plays), 1)

    def test_no_pick_when_fanduel_matches_the_sharp_price(self):
        _, ev = _eval((-180, 150), (-175, 155))
        self.assertIsNone(ev.recommended_side)
        self.assertIn("needs +2%", ev.dropped_reason)

    def test_no_sharp_price_means_no_pick(self):
        _, ev = _eval((-210, 175), None)
        self.assertIsNone(ev.recommended_side)

    def test_old_sharp_price_is_ignored_and_next_book_used(self):
        odds, _ = _eval((-210, 175), (-170, 150), sharp_seen="2026-10-28T19:00:00Z")
        self.assertIsNone(odds.sharp_home_prob)

    def test_too_good_to_be_real_is_skipped(self):
        _, ev = _eval((-400, 400), (-170, 150))
        self.assertIsNone(ev.recommended_side)
        self.assertIn("stale", ev.dropped_reason)


class _FakeDB:
    def get_recent_team_picks(self, *a, **k):
        return []

    def get_opening_line(self, *a, **k):
        return None

    def __getattr__(self, name):
        return lambda *a, **k: None


if __name__ == "__main__":
    unittest.main()
