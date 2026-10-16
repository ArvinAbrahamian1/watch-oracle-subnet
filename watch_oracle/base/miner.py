import logging
import time
from abc import abstractmethod
from typing import Optional

import bittensor as bt

from watch_oracle.base.neuron import BaseNeuron
from watch_oracle.protocol import FloorPrediction
from watch_oracle.transport import MinerServer
from watch_oracle.utils.config import Config, add_miner_args

logger = logging.getLogger(__name__)

BLOCK_TIME_SECONDS = 12


class BaseMinerNeuron(BaseNeuron):
    """
    Publishes the axon endpoint on chain, serves signed FloorPrediction requests,
    and keeps the metagraph fresh. Subclasses implement `forward`.
    """

    neuron_type: str = "MinerNeuron"

    @classmethod
    def add_args(cls, parser):
        super().add_args(parser)
        add_miner_args(cls, parser)

    def __init__(self, config: Optional[Config] = None):
        super().__init__(config=config)

        if not self.config.blacklist.force_validator_permit:
            logger.warning("Accepting requests from non-validators. Consider --blacklist.force_validator_permit.")
        if self.config.blacklist.allow_non_registered:
            logger.warning("Accepting requests from non-registered hotkeys. This is unsafe.")

        self.server = MinerServer(
            wallet=self.wallet,
            host=self.config.axon.ip,
            port=self.config.axon.port,
            handle=self.forward,
            gate=self.blacklist,
        )

    @abstractmethod
    def forward(self, request: FloorPrediction) -> FloorPrediction: ...

    def blacklist(self, hotkey: str) -> Optional[str]:
        """Rejection reason for `hotkey`, or None to accept. Runs after signature verification."""
        neuron = self.metagraph.by_hotkey(hotkey)
        if neuron is None:
            return None if self.config.blacklist.allow_non_registered else "unrecognized_hotkey"
        if self.config.blacklist.force_validator_permit and not neuron.validator_permit:
            return "no_validator_permit"
        return None

    def serve_axon(self) -> None:
        """Publish ip:port on chain, skipping the extrinsic when it is already current."""
        ip, port = self.config.axon.external_ip, self.config.axon.external_port
        if self.metagraph.neuron(self.uid).axon == f"{ip}:{port}":
            logger.info("Axon %s:%s already published", ip, port)
            return
        try:
            result = self.subtensor.execute(
                bt.ServeAxon(netuid=self.config.netuid, ip=ip, port=port), self.wallet
            )
        except bt.BittensorError as e:
            logger.error("Publishing axon %s:%s failed: %s", ip, port, e)
            raise SystemExit(1)
        if not result.success:
            logger.error("Publishing axon %s:%s failed: %s", ip, port, result.message)
            raise SystemExit(1)
        logger.info("Published axon %s:%s", ip, port)

    def run(self):
        self.serve_axon()
        self.server.start()
        logger.info("Miner started at block %s", self.block)
        try:
            while True:
                time.sleep(BLOCK_TIME_SECONDS)
                self.sync()
                self.step += 1
        except KeyboardInterrupt:
            logger.info("Miner stopped by keyboard interrupt.")
        finally:
            self.server.stop()
