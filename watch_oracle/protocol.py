"""
Validator -> miner wire protocol.

bittensor 11 has no Synapse/Axon/Dendrite. A query is a hotkey-signed HTTP POST
(bittensor.http_auth, btauth/1) of a JSON-encoded FloorPrediction to ENDPOINT on
the miner's served axon. The miner returns the same object with `predictions` filled.
"""

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from watch_oracle.utils.windows import MAX_WINDOW_DAYS, MIN_WINDOW_DAYS, is_valid_window_days

ENDPOINT = "/floor_prediction"
MAX_BODY_BYTES = 1 << 20


class ProtocolError(ValueError):
    """The body is not a structurally valid FloorPrediction."""


@dataclass
class FloorPrediction:
    """
    The validator fills `refs`, `horizon_days` and `window_days`. The miner fills
    `predictions` with a USD floor for every ref it is willing to predict, over the
    `window_days` window containing now + horizon_days. Refs the miner cannot
    predict should be omitted rather than guessed.

    Parsing checks structure only. Prediction values are validated by the
    validator, which treats any non-finite or non-positive value as malformed.
    """

    # Filled by the validator.
    refs: List[str] = field(default_factory=list)
    horizon_days: int = 7
    window_days: int = 7

    # Filled by the miner: ref -> predicted floor in USD.
    predictions: Optional[Dict[str, Any]] = None

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self), separators=(",", ":")).encode()

    @classmethod
    def from_bytes(cls, raw: bytes) -> "FloorPrediction":
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as e:
            raise ProtocolError(f"invalid JSON: {e}") from None
        if not isinstance(data, dict):
            raise ProtocolError("body must be a JSON object")

        refs = data.get("refs", [])
        horizon_days = data.get("horizon_days", 7)
        window_days = data.get("window_days", 7)
        predictions = data.get("predictions")
        if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
            raise ProtocolError("refs must be a list of strings")
        if isinstance(horizon_days, bool) or not isinstance(horizon_days, int):
            raise ProtocolError("horizon_days must be an integer")
        if not is_valid_window_days(window_days):
            raise ProtocolError(f"window_days must be an integer from {MIN_WINDOW_DAYS} to {MAX_WINDOW_DAYS}")
        if predictions is not None and not isinstance(predictions, dict):
            raise ProtocolError("predictions must be an object")
        return cls(refs=refs, horizon_days=horizon_days, window_days=window_days, predictions=predictions)
