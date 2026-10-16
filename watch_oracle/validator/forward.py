"""
Validator cycle.

Every cycle:
  1. If a query round is due, sample refs from the cached universe, query all
     queryable miners with FloorPrediction, and store every (miner, ref) prediction.
  2. Score all stored predictions whose target window has fully closed, fetching
     each (ref, window) ground truth from the oracle at most once.
  3. Recompute per-uid scores from the rolling window.
"""

import logging
import math
import random
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Dict, List, Optional

from watch_oracle.oracle_client import OracleAuthError, OracleClient, OracleError
from watch_oracle.protocol import FloorPrediction
from watch_oracle.transport import query_miners
from watch_oracle.utils.uids import get_miner_targets
from watch_oracle.utils.windows import (
    latest_closed_window_start,
    rolling_window_start,
    target_window_start,
    utc_now,
)
from watch_oracle.validator.reward import score_predictions
from watch_oracle.validator.storage import PredictionRecord

if TYPE_CHECKING:
    from neurons.validator import Validator

logger = logging.getLogger(__name__)


class UniverseCache:
    """Oracle universe, refetched at most once per `ttl`. Falls back to the stale copy on error."""

    def __init__(self, oracle: OracleClient, ttl: timedelta):
        self._oracle = oracle
        self._ttl = ttl
        self._refs: List[str] = []
        self._fetched_at: Optional[datetime] = None

    def refs(self, now: datetime) -> List[str]:
        if self._fetched_at is None or now - self._fetched_at >= self._ttl:
            try:
                self._refs = [u.ref for u in self._oracle.get_universe()]
                self._fetched_at = now
                logger.info("Universe refreshed: %s refs", len(self._refs))
            except OracleError as e:
                logger.error("Universe fetch failed, using cached copy of %s refs: %s", len(self._refs), e)
        return self._refs


def forward(self: "Validator") -> None:
    now = utc_now()
    if _round_due(self, now):
        query_round(self, now)
    score_closed_predictions(self, now)
    self.refresh_scores(now)


def _round_due(self: "Validator", now: datetime) -> bool:
    last = self.store.last_round_at()
    return last is None or (now - last).total_seconds() >= self.config.neuron.round_interval


def query_round(self: "Validator", now: datetime) -> None:
    universe = self.universe.refs(now)
    if not universe:
        logger.warning("Universe is empty, skipping query round.")
        return
    refs = random.sample(universe, min(self.config.neuron.batch_size, len(universe)))

    targets = get_miner_targets(self.metagraph, self.config.neuron.vpermit_tao_limit, exclude=[self.uid])
    if not targets:
        logger.warning("No queryable miners, skipping query round.")
        return

    n = self.config.neuron
    responses = query_miners(
        self.wallet,
        targets,
        FloorPrediction(refs=refs, horizon_days=n.horizon_days, window_days=n.window_days),
        timeout=n.timeout,
    )

    round_id = uuid.uuid4().hex
    window = target_window_start(now, n.horizon_days, n.window_days)
    records = []
    failed = 0
    for target in targets:
        predictions = extract_predictions(responses.get(target.uid), refs)
        if predictions is None:
            failed += 1
            predictions = {}
        records.extend(
            PredictionRecord(
                round_id=round_id,
                miner_uid=target.uid,
                miner_hotkey=target.hotkey,
                ref=ref,
                predicted_floor=predictions.get(ref),
                target_window_start=window,
                window_days=n.window_days,
                created_at=now,
            )
            for ref in refs
        )
    self.store.insert_predictions(records)
    logger.info(
        "Round %s: %s refs, %s miners queried, %s no response or malformed, target window %s (%sd)",
        round_id,
        len(refs),
        len(targets),
        failed,
        window,
        n.window_days,
    )


def extract_predictions(
    response: Optional[FloorPrediction], refs: List[str]
) -> Optional[Dict[str, float]]:
    """
    Usable predictions for the requested refs, or None if the miner did not respond
    or returned a malformed response (in which case every ref scores 0).
    Unrequested refs are ignored. Any non-finite or non-positive value is malformed.
    """
    if response is None or not isinstance(response.predictions, dict):
        return None
    requested = set(refs)
    out = {}
    for ref, value in response.predictions.items():
        if ref not in requested:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(value) or value <= 0:
            return None
        out[ref] = float(value)
    return out


def score_closed_predictions(self: "Validator", now: datetime) -> None:
    n = self.config.neuron
    pending = self.store.unscored_predictions(
        window_days=n.window_days,
        min_start=rolling_window_start(now, n.window_days, n.rolling_window_days),
        max_start=latest_closed_window_start(now, n.window_days),
    )
    if not pending:
        return

    keys = {(p.ref, p.target_window_start) for p in pending}
    truth = self.store.ground_truth(keys, n.window_days)
    for ref, start in sorted(keys - truth.keys()):
        try:
            floor = self.oracle.get_floor(ref, start, n.window_days)
        except OracleAuthError as e:
            logger.error("Stopping scoring pass: %s", e)
            break
        except OracleError as e:
            logger.warning("Ground truth for %s %s unavailable, retrying next cycle: %s", ref, start, e)
            continue
        self.store.put_ground_truth(
            ref,
            start,
            n.window_days,
            floor.floor_usd if floor else None,
            floor.sample_count if floor else None,
            fetched_at=now,
        )
        truth[(ref, start)] = floor.floor_usd if floor else None

    scores = score_predictions(pending, truth, n.error_band)
    self.store.insert_scores(scores, scored_at=now)
    excluded = sum(1 for s in scores if s.score is None)
    logger.info(
        "Scored %s of %s pending predictions (%s excluded, no ground truth)", len(scores), len(pending), excluded
    )
