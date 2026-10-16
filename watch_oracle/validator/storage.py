"""
SQLite persistence for the validator. Fresh databases only, no migrations.

predictions   one row per (round, miner, ref). predicted_floor is NULL when the miner
              gave no usable prediction for that ref (no response, malformed response,
              or ref omitted), which scores 0 once ground truth exists.
ground_truth  one row per (ref, window_start, window_days). floor_usd is NULL when the
              oracle returned 404.
scores        one row per scored prediction. score is NULL when the ref was excluded
              because ground truth was unavailable.
round_scores  view: a miner's round score is the mean of its non-excluded scores.

window_days is stored on every row and every query filters on it, so changing
--neuron.window_days never mixes floors or scores from different window lengths.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional, Set, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id            TEXT    NOT NULL,
    miner_uid           INTEGER NOT NULL,
    miner_hotkey        TEXT    NOT NULL,
    ref                 TEXT    NOT NULL,
    predicted_floor     REAL,
    target_window_start TEXT    NOT NULL,
    window_days         INTEGER NOT NULL,
    created_at          TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_predictions_target_window
    ON predictions (window_days, target_window_start);

CREATE TABLE IF NOT EXISTS ground_truth (
    ref          TEXT    NOT NULL,
    window_start TEXT    NOT NULL,
    window_days  INTEGER NOT NULL,
    floor_usd    REAL,
    sample_count INTEGER,
    fetched_at   TEXT    NOT NULL,
    PRIMARY KEY (ref, window_start, window_days)
);

CREATE TABLE IF NOT EXISTS scores (
    prediction_id INTEGER PRIMARY KEY REFERENCES predictions (id),
    error         REAL,
    score         REAL,
    scored_at     TEXT NOT NULL
);

CREATE VIEW IF NOT EXISTS round_scores AS
SELECT p.round_id,
       p.miner_uid,
       p.miner_hotkey,
       p.target_window_start,
       p.window_days,
       AVG(s.score)   AS round_score,
       COUNT(s.score) AS refs_scored
FROM scores s
JOIN predictions p ON p.id = s.prediction_id
WHERE s.score IS NOT NULL
GROUP BY p.round_id, p.miner_uid, p.miner_hotkey, p.target_window_start, p.window_days;
"""

GroundTruthKey = Tuple[str, date]
MinerKey = Tuple[int, str]


@dataclass(frozen=True)
class PredictionRecord:
    round_id: str
    miner_uid: int
    miner_hotkey: str
    ref: str
    predicted_floor: Optional[float]
    target_window_start: date
    window_days: int
    created_at: datetime


@dataclass(frozen=True)
class PendingPrediction:
    id: int
    ref: str
    predicted_floor: Optional[float]
    target_window_start: date


@dataclass(frozen=True)
class ScoreRecord:
    prediction_id: int
    error: Optional[float]
    score: Optional[float]


class ValidatorStore:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def insert_predictions(self, records: Iterable[PredictionRecord]) -> None:
        with self._conn:
            self._conn.executemany(
                "INSERT INTO predictions (round_id, miner_uid, miner_hotkey, ref, predicted_floor,"
                " target_window_start, window_days, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        r.round_id,
                        r.miner_uid,
                        r.miner_hotkey,
                        r.ref,
                        r.predicted_floor,
                        r.target_window_start.isoformat(),
                        r.window_days,
                        r.created_at.isoformat(),
                    )
                    for r in records
                ],
            )

    def last_round_at(self) -> Optional[datetime]:
        row = self._conn.execute("SELECT MAX(created_at) FROM predictions").fetchone()
        return datetime.fromisoformat(row[0]) if row[0] else None

    def unscored_predictions(
        self, window_days: int, min_start: date, max_start: date
    ) -> List[PendingPrediction]:
        """Predictions for `window_days` windows starting in [min_start, max_start] with no score row."""
        rows = self._conn.execute(
            "SELECT p.id, p.ref, p.predicted_floor, p.target_window_start FROM predictions p"
            " LEFT JOIN scores s ON s.prediction_id = p.id"
            " WHERE s.prediction_id IS NULL AND p.window_days = ?"
            " AND p.target_window_start BETWEEN ? AND ?",
            (window_days, min_start.isoformat(), max_start.isoformat()),
        ).fetchall()
        return [
            PendingPrediction(
                id=r[0], ref=r[1], predicted_floor=r[2], target_window_start=date.fromisoformat(r[3])
            )
            for r in rows
        ]

    def ground_truth(
        self, keys: Set[GroundTruthKey], window_days: int
    ) -> Dict[GroundTruthKey, Optional[float]]:
        """Cached floors for `keys`. Missing keys are not cached; None means the oracle returned 404."""
        starts = sorted({start.isoformat() for _, start in keys})
        if not starts:
            return {}
        placeholders = ",".join("?" * len(starts))
        rows = self._conn.execute(
            "SELECT ref, window_start, floor_usd FROM ground_truth"
            f" WHERE window_days = ? AND window_start IN ({placeholders})",
            [window_days, *starts],
        ).fetchall()
        cached = {(r[0], date.fromisoformat(r[1])): r[2] for r in rows}
        return {k: v for k, v in cached.items() if k in keys}

    def put_ground_truth(
        self,
        ref: str,
        window_start: date,
        window_days: int,
        floor_usd: Optional[float],
        sample_count: Optional[int],
        fetched_at: datetime,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO ground_truth"
                " (ref, window_start, window_days, floor_usd, sample_count, fetched_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (ref, window_start.isoformat(), window_days, floor_usd, sample_count, fetched_at.isoformat()),
            )

    def insert_scores(self, records: Iterable[ScoreRecord], scored_at: datetime) -> None:
        with self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO scores (prediction_id, error, score, scored_at) VALUES (?, ?, ?, ?)",
                [(r.prediction_id, r.error, r.score, scored_at.isoformat()) for r in records],
            )

    def rolling_scores(self, window_days: int, min_start: date) -> Dict[MinerKey, float]:
        """Mean round score per (uid, hotkey) over `window_days` rounds whose target starts >= min_start."""
        rows = self._conn.execute(
            "SELECT miner_uid, miner_hotkey, AVG(round_score) FROM round_scores"
            " WHERE window_days = ? AND target_window_start >= ?"
            " GROUP BY miner_uid, miner_hotkey",
            (window_days, min_start.isoformat()),
        ).fetchall()
        return {(r[0], r[1]): float(r[2]) for r in rows}
