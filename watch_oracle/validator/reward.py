"""
Scoring.

  error = |predicted - floor_usd| / floor_usd
  score = max(0, 1 - error / error_band)

A missing prediction scores 0. A ref with no ground truth (oracle 404) is excluded
(score NULL), so it neither helps nor hurts. Round scores and the rolling average are
aggregated in SQL (see storage.round_scores and ValidatorStore.rolling_scores).
"""

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from watch_oracle.validator.storage import (
    GroundTruthKey,
    MinerKey,
    PendingPrediction,
    ScoreRecord,
)


def score_prediction(
    predicted: Optional[float], floor_usd: float, error_band: float
) -> Tuple[Optional[float], float]:
    """Return (error, score). error is None when there was no prediction."""
    if predicted is None:
        return None, 0.0
    error = abs(predicted - floor_usd) / floor_usd
    return error, max(0.0, 1.0 - error / error_band)


def score_predictions(
    pending: Iterable[PendingPrediction],
    ground_truth: Dict[GroundTruthKey, Optional[float]],
    error_band: float,
) -> List[ScoreRecord]:
    """Score every pending prediction whose ground truth is known. Others are skipped."""
    records = []
    for p in pending:
        key = (p.ref, p.target_window_start)
        if key not in ground_truth:
            continue
        floor_usd = ground_truth[key]
        if floor_usd is None:
            records.append(ScoreRecord(prediction_id=p.id, error=None, score=None))
            continue
        error, score = score_prediction(p.predicted_floor, floor_usd, error_band)
        records.append(ScoreRecord(prediction_id=p.id, error=error, score=score))
    return records


def scores_vector(rolling: Dict[MinerKey, float], hotkeys: Sequence[str]) -> List[float]:
    """
    Per-uid weights from rolling scores. A uid only inherits history recorded under
    its current hotkey, so a re-registered uid starts at 0.
    """
    return [rolling.get((uid, hotkey), 0.0) for uid, hotkey in enumerate(hotkeys)]
