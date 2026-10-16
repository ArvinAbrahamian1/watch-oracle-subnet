import logging
import time
from abc import abstractmethod
from typing import List, Optional

import bittensor as bt

from watch_oracle.base.neuron import BaseNeuron
from watch_oracle.utils.config import Config, add_validator_args

logger = logging.getLogger(__name__)


class BaseValidatorNeuron(BaseNeuron):
    """
    Runs the forward / sync loop and sets weights from `self.scores`.

    `self.scores` is a per-uid list that subclasses keep up to date. bt.SetWeights
    normalizes, clips to max_weight_limit and quantizes on submit, so only relative
    values matter.
    """

    neuron_type: str = "ValidatorNeuron"

    @classmethod
    def add_args(cls, parser):
        super().add_args(parser)
        add_validator_args(cls, parser)

    def __init__(self, config: Optional[Config] = None):
        super().__init__(config=config)
        self.scores: List[float] = [0.0] * len(self.metagraph.neurons)
        self._last_weights_block = self.metagraph.neuron(self.uid).last_update

    @abstractmethod
    def forward(self): ...

    def run(self):
        self.sync()
        logger.info("Validator started at block %s", self.block)
        try:
            while True:
                logger.info("step(%s) block(%s)", self.step, self.block)
                self.forward()
                self.sync()
                self.step += 1
                time.sleep(self.config.neuron.cycle_interval)
        except KeyboardInterrupt:
            logger.info("Validator stopped by keyboard interrupt.")

    def should_set_weights(self) -> bool:
        if self.config.neuron.disable_set_weights or self.step == 0:
            return False
        last = max(self._last_weights_block, self.metagraph.neuron(self.uid).last_update)
        return self.block - last > self.config.neuron.epoch_length

    def set_weights(self):
        weighted = [(uid, score) for uid, score in enumerate(self.scores) if score > 0]
        # bt.SetWeights rejects an all-zero vector. Log once per attempt instead of erroring.
        if not weighted:
            logger.warning("No miner has a positive rolling score yet, not setting weights.")
            return

        intent = bt.SetWeights(
            netuid=self.config.netuid,
            uids=[uid for uid, _ in weighted],
            weights=[score for _, score in weighted],
            version_key=self.spec_version,
        )
        try:
            result = self.subtensor.execute(intent, self.wallet)
        except bt.BittensorError as e:
            logger.error("set_weights failed: %s", e)
            return
        if result.success:
            self._last_weights_block = self.block
            logger.info("set_weights succeeded for %s uids", len(weighted))
        else:
            logger.error("set_weights failed: %s", result.message)
