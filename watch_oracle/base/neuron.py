import logging
import os
from abc import ABC, abstractmethod
from typing import List, Optional

import bittensor as bt

from watch_oracle import __spec_version__
from watch_oracle.utils.config import Config, add_args, build_config, check_config, setup_logging

logger = logging.getLogger(__name__)


class BaseNeuron(ABC):
    """
    Shared wallet, subtensor and metagraph wiring for miners and validators.
    Subclasses implement `run` and, for validators, `set_weights`.

    The metagraph is an immutable snapshot in bittensor 11, so a resync replaces it.
    """

    neuron_type: str = "BaseNeuron"
    spec_version: int = __spec_version__

    @classmethod
    def add_args(cls, parser):
        add_args(cls, parser)

    @classmethod
    def check_config(cls, config: Config):
        check_config(cls, config)

    @classmethod
    def config(cls, args: Optional[List[str]] = None) -> Config:
        return build_config(cls, args)

    @property
    def block(self) -> int:
        return self.subtensor.block

    def __init__(self, config: Optional[Config] = None):
        self.config = config if config is not None else self.config()
        self.check_config(self.config)
        setup_logging(self.config)
        logger.info("Config: %s", self.config)

        self.wallet = bt.Wallet(
            name=self.config.wallet.name,
            hotkey=self.config.wallet.hotkey,
            path=os.path.expanduser(self.config.wallet.path),
        )
        self.hotkey = self.wallet.hotkey.ss58_address
        self.subtensor = bt.Subtensor(self.config.subtensor.network)
        self.metagraph = self._fetch_metagraph()
        self.uid = self.check_registered()
        logger.info(
            "Running %s on %s netuid %s uid %s hotkey %s",
            self.neuron_type,
            self.config.subtensor.network,
            self.config.netuid,
            self.uid,
            self.hotkey,
        )
        self.step = 0
        self._last_sync_block = self.block

    @abstractmethod
    def run(self): ...

    def sync(self):
        """Resync the metagraph every epoch, and set weights if this neuron is due to."""
        self.uid = self.check_registered()
        if self.should_sync_metagraph():
            self.resync_metagraph()
        if self.should_set_weights():
            self.set_weights()

    def check_registered(self) -> int:
        uid = self.subtensor.neurons.uid(self.hotkey, self.config.netuid)
        if uid is None:
            logger.error(
                "Hotkey %s is not registered on netuid %s. Run `btcli subnets register` and try again.",
                self.hotkey,
                self.config.netuid,
            )
            raise SystemExit(1)
        return uid

    def _fetch_metagraph(self) -> "bt.Metagraph":
        metagraph = self.subtensor.subnets.metagraph(self.config.netuid, commitments=False)
        if metagraph is None:
            logger.error("Subnet %s does not exist on %s", self.config.netuid, self.config.subtensor.network)
            raise SystemExit(1)
        return metagraph

    def should_sync_metagraph(self) -> bool:
        return self.block - self._last_sync_block >= self.config.neuron.epoch_length

    def resync_metagraph(self):
        logger.info("Resyncing metagraph")
        self.metagraph = self._fetch_metagraph()
        self._last_sync_block = self.block

    def should_set_weights(self) -> bool:
        return False

    def set_weights(self):
        raise NotImplementedError
