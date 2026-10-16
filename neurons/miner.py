import logging
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Dict, List, Optional, Tuple

from watch_oracle.base.miner import BaseMinerNeuron
from watch_oracle.oracle_client import OracleClient, OracleError
from watch_oracle.protocol import FloorPrediction
from watch_oracle.utils.config import Config
from watch_oracle.utils.windows import latest_closed_window_start, utc_now

logger = logging.getLogger(__name__)

ORACLE_CONCURRENCY = 8

# (window_start, window_days)
WindowKey = Tuple[date, int]


class Miner(BaseMinerNeuron):
    """
    Baseline miner: predicts the target window's floor as the latest closed window's floor,
    using the window length the validator sends in each request.

    NOTE: This baseline reads the ground truth oracle (ORACLE_API_KEY) so the subnet
    can be exercised end to end on testnet. Real miners will NOT have ground truth
    access and must bring their own model and data.
    """

    def __init__(self, config: Optional[Config] = None):
        super().__init__(config=config)
        self.oracle = OracleClient()
        self.noise = self.config.miner.noise
        self._rng = random.Random()
        self._pool = ThreadPoolExecutor(max_workers=ORACLE_CONCURRENCY)
        # Closed windows never change, so floors are cached per (window_start, window_days).
        # Only the latest closed window is kept for each length, so at most 7 buckets exist.
        self._cache_lock = threading.Lock()
        self._floor_cache: Dict[WindowKey, Dict[str, Optional[float]]] = {}

    def forward(self, request: FloorPrediction) -> FloorPrediction:
        key = (latest_closed_window_start(utc_now(), request.window_days), request.window_days)
        floors = self._floors_for(request.refs, key)
        request.predictions = {
            ref: self._apply_noise(floor) for ref, floor in floors.items() if floor is not None
        }
        logger.info(
            "Predicted %s/%s refs (horizon %sd, base window %s, %sd)",
            len(request.predictions),
            len(request.refs),
            request.horizon_days,
            key[0],
            key[1],
        )
        return request

    def _floors_for(self, refs: List[str], key: WindowKey) -> Dict[str, Optional[float]]:
        start, days = key
        with self._cache_lock:
            for stale in [k for k in self._floor_cache if k[1] == days and k[0] != start]:
                del self._floor_cache[stale]
            bucket = self._floor_cache.setdefault(key, {})
            missing = [ref for ref in dict.fromkeys(refs) if ref not in bucket]

        for ref, found, floor in self._pool.map(lambda r: self._fetch_floor(r, start, days), missing):
            if found:
                with self._cache_lock:
                    bucket[ref] = floor

        with self._cache_lock:
            return {ref: bucket.get(ref) for ref in refs}

    def _fetch_floor(self, ref: str, start: date, days: int) -> Tuple[str, bool, Optional[float]]:
        """Return (ref, cacheable, floor). 404 is cacheable as None, other errors are not cached."""
        try:
            result = self.oracle.get_floor(ref, start, days)
        except OracleError as e:
            logger.warning("Floor lookup failed for %s %s (%sd): %s", ref, start, days, e)
            return ref, False, None
        return ref, True, result.floor_usd if result else None

    def _apply_noise(self, floor: float) -> float:
        if self.noise == 0:
            return floor
        return floor * (1.0 + self._rng.uniform(-self.noise, self.noise))


if __name__ == "__main__":
    Miner().run()
