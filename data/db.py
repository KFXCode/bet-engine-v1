"""
data/db.py
==========
Single SQLite database for the whole engine: games, odds snapshots, public
betting splits, recommendations, results, and bankroll history.
"""

import sqlite3
import json
from contextlib import contextmanager

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    home_pitcher TEXT,
    away_pitcher TEXT,
    game_time_utc TEXT
);

CREATE TABLE IF NOT EXISTS odds_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    book TEXT NOT NULL,
    home_ml INTEGER,
    away_ml INTEGER,
    home_spread REAL,
    away_spread REAL,
    total REAL,
    is_opening INTEGER DEFAULT 0,
    FOREIGN KEY (game_id) REFERENCES games (game_id)
);

CREATE TABLE IF NOT EXISTS public_splits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    tickets_pct_home REAL,
    handle_pct_home REAL,
    source TEXT,
    data_quality TEXT,
    FOREIGN KEY (game_id) REFERENCES games (game_id)
);

CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    game_id TEXT,
    kind TEXT NOT NULL DEFAULT 'moneyline',
    side_or_player TEXT NOT NULL,
    team TEXT,
    sport TEXT NOT NULL DEFAULT 'MLB',
    odds_american INTEGER,
    edge_pct REAL,
    model_prob REAL,
    market_prob REAL,
    stake_units REAL,
    stake_dollars REAL,
    reasoning_json TEXT,
    factor_scores_json TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    clv_pct REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS results (
    game_id TEXT PRIMARY KEY,
    home_score INTEGER,
    away_score INTEGER,
    final INTEGER DEFAULT 0,
    graded_at TEXT
);

CREATE TABLE IF NOT EXISTS bankroll_log (
    date TEXT PRIMARY KEY,
    units_staked REAL DEFAULT 0,
    units_won REAL DEFAULT 0,
    dollars_staked REAL DEFAULT 0,
    dollars_won REAL DEFAULT 0,
    running_bankroll REAL,
    bets_graded INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stats_cache (
    key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    cached_at REAL NOT NULL
);
"""


class Database:
    def __init__(self, path=None):
        self.path = str(path or DB_PATH)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self):
        """Add columns introduced after the DB was first created. SQLite
        can't add a column via CREATE TABLE IF NOT EXISTS, so add-if-missing
        here. Safe to run every startup."""
        cur = self._conn.cursor()
        cur.execute("PRAGMA table_info(recommendations)")
        cols = {r[1] for r in cur.fetchall()}
        if "clv_pct" not in cols:
            cur.execute("ALTER TABLE recommendations ADD COLUMN clv_pct REAL")
        if "sport" not in cols:
            cur.execute("ALTER TABLE recommendations ADD COLUMN sport TEXT NOT NULL DEFAULT 'MLB'")
        cur.close()

    @contextmanager
    def cursor(self):
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        finally:
            cur.close()

    def upsert_game(self, game):
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO games (game_id, date, home_team, away_team,
                       home_pitcher, away_pitcher, game_time_utc)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(game_id) DO UPDATE SET
                       home_pitcher=excluded.home_pitcher,
                       away_pitcher=excluded.away_pitcher,
                       game_time_utc=excluded.game_time_utc""",
                (game.game_id, game.date, game.home_team, game.away_team,
                 game.home_pitcher.name if game.home_pitcher else None,
                 game.away_pitcher.name if game.away_pitcher else None,
                 game.game_time_utc),
            )

    def get_game(self, game_id):
        """Stored schedule row for a game (date + team abbreviations). The
        grader needs this to settle NON-MLB picks: their game_ids are hashes,
        not ESPN event ids, so results are matched on date + teams instead."""
        with self.cursor() as cur:
            cur.execute("SELECT * FROM games WHERE game_id=?", (game_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def record_odds_snapshot(self, game_id, odds, captured_at, is_opening=False):
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO odds_snapshots
                       (game_id, captured_at, book, home_ml, away_ml,
                        home_spread, away_spread, total, is_opening)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (game_id, captured_at, odds.book, odds.home_ml, odds.away_ml,
                 odds.home_spread, odds.away_spread, odds.total, int(is_opening)),
            )

    def get_opening_line(self, game_id):
        with self.cursor() as cur:
            cur.execute(
                """SELECT * FROM odds_snapshots WHERE game_id=? AND is_opening=1
                   ORDER BY captured_at ASC LIMIT 1""",
                (game_id,),
            )
            return cur.fetchone()

    def get_latest_line(self, game_id):
        with self.cursor() as cur:
            cur.execute(
                """SELECT * FROM odds_snapshots WHERE game_id=?
                   ORDER BY captured_at DESC LIMIT 1""",
                (game_id,),
            )
            return cur.fetchone()

    def get_last_real_line(self, game_id):
        """Most recent NON-mock (real sportsbook) line for a game. Used to
        restore the true pre-game FanDuel odds once a game starts and drops
        out of the live feed, instead of showing synthetic mock numbers."""
        with self.cursor() as cur:
            cur.execute(
                """SELECT * FROM odds_snapshots WHERE game_id=? AND book!='mock'
                   ORDER BY captured_at DESC LIMIT 1""",
                (game_id,),
            )
            return cur.fetchone()

    def record_public_split(self, game_id, split, captured_at):
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO public_splits
                       (game_id, captured_at, tickets_pct_home, handle_pct_home,
                        source, data_quality)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (game_id, captured_at, split.tickets_pct_home, split.handle_pct_home,
                 split.source, split.data_quality),
            )

    def insert_recommendation(self, date, game_id, kind, side_or_player, team,
                               odds_american, edge_pct, model_prob, market_prob,
                               stake_units, stake_dollars, reasoning, factor_scores,
                               created_at, sport="MLB"):
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO recommendations
                       (date, game_id, kind, side_or_player, team, sport, odds_american,
                        edge_pct, model_prob, market_prob, stake_units, stake_dollars,
                        reasoning_json, factor_scores_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (date, game_id, kind, side_or_player, team, sport, odds_american, edge_pct,
                 model_prob, market_prob, stake_units, stake_dollars,
                 json.dumps(reasoning), json.dumps(factor_scores), created_at),
            )

    def get_recent_team_picks(self, before_date, lookback_days, kind="moneyline"):
        with self.cursor() as cur:
            cur.execute(
                """SELECT date, team FROM recommendations
                   WHERE kind=? AND date < ?
                   ORDER BY date DESC LIMIT ?""",
                (kind, before_date, lookback_days),
            )
            return [dict(r) for r in cur.fetchall()]

    def delete_pending_recommendations_for_date(self, date):
        with self.cursor() as cur:
            cur.execute("DELETE FROM recommendations WHERE date=? AND status='pending'", (date,))

    def get_pending_recommendations(self, date=None):
        with self.cursor() as cur:
            if date:
                cur.execute("SELECT * FROM recommendations WHERE status='pending' AND date=?", (date,))
            else:
                cur.execute("SELECT * FROM recommendations WHERE status='pending'")
            return [dict(r) for r in cur.fetchall()]

    def set_recommendation_status(self, rec_id, status):
        with self.cursor() as cur:
            cur.execute("UPDATE recommendations SET status=? WHERE id=?", (status, rec_id))

    def set_recommendation_clv(self, rec_id, clv_pct):
        with self.cursor() as cur:
            cur.execute("UPDATE recommendations SET clv_pct=? WHERE id=?", (clv_pct, rec_id))

    def get_record_by_kind(self, kind, after=None, sport=None):
        q = "SELECT status, COUNT(*) c FROM recommendations WHERE kind=? AND status IN ('won','lost','push')"
        params = [kind]
        if after:
            q += " AND date > ?"
            params.append(after)
        if sport:
            q += " AND sport = ?"
            params.append(sport)
        q += " GROUP BY status"
        with self.cursor() as cur:
            cur.execute(q, params)
            counts = {r["status"]: r["c"] for r in cur.fetchall()}
        return {"wins": counts.get("won", 0), "losses": counts.get("lost", 0), "pushes": counts.get("push", 0)}

    def get_clv_summary(self, kind="moneyline"):
        with self.cursor() as cur:
            cur.execute(
                "SELECT clv_pct FROM recommendations WHERE kind=? AND clv_pct IS NOT NULL",
                (kind,),
            )
            vals = [r["clv_pct"] for r in cur.fetchall() if r["clv_pct"] is not None]
        if not vals:
            return {"n": 0, "avg_clv_pct": None, "beat_pct": None}
        beat = sum(1 for v in vals if v > 0)
        return {"n": len(vals),
                "avg_clv_pct": round(sum(vals) / len(vals), 2),
                "beat_pct": round(100.0 * beat / len(vals), 0)}

    def get_first_graded_date(self, kind):
        with self.cursor() as cur:
            cur.execute(
                "SELECT MIN(date) d FROM recommendations WHERE kind=? AND status IN ('won','lost','push')",
                (kind,),
            )
            row = cur.fetchone()
            return row["d"] if row and row["d"] else None

    def get_recommendations_for_date(self, date, kind=None):
        with self.cursor() as cur:
            if kind:
                cur.execute("SELECT * FROM recommendations WHERE date=? AND kind=? ORDER BY id", (date, kind))
            else:
                cur.execute("SELECT * FROM recommendations WHERE date=? ORDER BY id", (date,))
            return [dict(r) for r in cur.fetchall()]

    def get_last_slate_date(self, before_date):
        with self.cursor() as cur:
            cur.execute(
                "SELECT MAX(date) d FROM recommendations WHERE date < ? AND status IN ('won','lost','push')",
                (before_date,),
            )
            row = cur.fetchone()
            return row["d"] if row and row["d"] else None

    def get_graded_history(self, after=None):
        q = ("SELECT date, kind, team, side_or_player, sport, odds_american, status "
             "FROM recommendations WHERE status IN ('won','lost','push')")
        params = []
        if after:
            q += " AND date > ?"
            params.append(after)
        q += " ORDER BY date DESC, id ASC"
        with self.cursor() as cur:
            cur.execute(q, params)
            return [dict(r) for r in cur.fetchall()]

    def record_result(self, game_id, home_score, away_score, graded_at):
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO results (game_id, home_score, away_score, final, graded_at)
                   VALUES (?, ?, ?, 1, ?)
                   ON CONFLICT(game_id) DO UPDATE SET
                       home_score=excluded.home_score, away_score=excluded.away_score,
                       final=1, graded_at=excluded.graded_at""",
                (game_id, home_score, away_score, graded_at),
            )

    def upsert_bankroll_day(self, date, **fields):
        existing = self.get_bankroll_day(date)
        merged = {**(existing or {}), **fields}
        merged["date"] = date
        cols = ["date", "units_staked", "units_won", "dollars_staked", "dollars_won",
                "running_bankroll", "bets_graded", "wins"]
        vals = [merged.get(c, 0) if c != "date" else date for c in cols]
        with self.cursor() as cur:
            placeholders = ",".join("?" for _ in cols)
            updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "date")
            cur.execute(
                f"""INSERT INTO bankroll_log ({",".join(cols)}) VALUES ({placeholders})
                    ON CONFLICT(date) DO UPDATE SET {updates}""",
                vals,
            )

    def get_bankroll_day(self, date):
        with self.cursor() as cur:
            cur.execute("SELECT * FROM bankroll_log WHERE date=?", (date,))
            row = cur.fetchone()
            return dict(row) if row else None

    def get_bankroll_history(self, limit=30):
        with self.cursor() as cur:
            cur.execute("SELECT * FROM bankroll_log ORDER BY date DESC LIMIT ?", (limit,))
            return [dict(r) for r in cur.fetchall()]

    def close(self):
        self._conn.close()
