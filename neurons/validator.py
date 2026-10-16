import logging
from datetime import datetime, timedelta
from typing import Optional

from watch_oracle.base.validator import BaseValidatorNeuron
from watch_oracle.oracle_client import OracleClient
from watch_oracle.utils.config import Config
from watch_oracle.utils.windows import rolling_window_start, utc_now
from watch_oracle.validator.forward import UniverseCache, forward
from watch_oracle.validator.reward import scores_vector
from watch_oracle.validator.storage import ValidatorStore

logger = logging.getLogger(__name__)


class Validator(BaseValidatorNeuron):
    def __init__(self, config: Optional[Config] = None):
        super().__init__(config=config)
        self.oracle = OracleClient()
        self.store = ValidatorStore(self.config.neuron.db_path)
        self.universe = UniverseCache(
            self.oracle, ttl=timedelta(seconds=self.config.neuron.universe_ttl)
        )
        logger.info("Oracle: %s, db: %s", self.oracle, self.config.neuron.db_path)
        self.refresh_scores(utc_now())

    def forward(self):
        forward(self)

    def resync_metagraph(self):
        super().resync_metagraph()
        self.refresh_scores(utc_now())

    def refresh_scores(self, now: datetime) -> None:
        n = self.config.neuron
        rolling = self.store.rolling_scores(
            n.window_days, rolling_window_start(now, n.window_days, n.rolling_window_days)
        )
        self.scores = scores_vector(rolling, self.metagraph.hotkeys)


if __name__ == "__main__":
    Validator().run()
