"""
Command line config.

bittensor 11 dropped bt.Config and the X.add_args(parser) helpers, so neurons parse
their own flags. Dotted flags (--neuron.horizon_days) become nested attributes
(config.neuron.horizon_days), matching the bittensor 9.x config shape.
"""

import argparse
import logging
import os
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from watch_oracle.utils.windows import MAX_WINDOW_DAYS, MIN_WINDOW_DAYS, is_valid_window_days


class Config(SimpleNamespace):
    """Nested namespace built from dotted argparse destinations."""


def build_config(cls, args: Optional[List[str]] = None) -> Config:
    parser = argparse.ArgumentParser()
    cls.add_args(parser)
    return _nest(vars(parser.parse_args(args)))


def _nest(flat: Dict[str, Any]) -> Config:
    root = Config()
    for key, value in flat.items():
        node = root
        *parents, leaf = key.split(".")
        for part in parents:
            if not hasattr(node, part):
                setattr(node, part, Config())
            node = getattr(node, part)
        setattr(node, leaf, value)
    return root


def setup_logging(config: Config) -> None:
    logging.basicConfig(
        level=config.logging.level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )


def check_config(cls, config: Config) -> None:
    """Validate the config and resolve derived paths. Raises ValueError on bad values."""
    full_path = os.path.expanduser(
        f"{config.neuron.state_dir}/{config.wallet.name}/{config.wallet.hotkey}"
        f"/netuid{config.netuid}/{config.neuron.name}"
    )
    config.neuron.full_path = full_path
    os.makedirs(full_path, exist_ok=True)

    if config.neuron.epoch_length < 1:
        raise ValueError("--neuron.epoch_length must be >= 1")

    if cls.neuron_type == "ValidatorNeuron":
        n = config.neuron
        if not is_valid_window_days(n.window_days):
            raise ValueError(f"--neuron.window_days must be between {MIN_WINDOW_DAYS} and {MAX_WINDOW_DAYS}")
        if n.horizon_days < 1:
            raise ValueError("--neuron.horizon_days must be >= 1")
        if n.batch_size < 1:
            raise ValueError("--neuron.batch_size must be >= 1")
        if n.error_band <= 0:
            raise ValueError("--neuron.error_band must be > 0")
        if n.rolling_window_days < n.window_days:
            raise ValueError("--neuron.rolling_window_days must be >= --neuron.window_days")
        if not n.db_path:
            n.db_path = os.path.join(full_path, "watch_oracle.db")

    if cls.neuron_type == "MinerNeuron":
        if not 0 <= config.miner.noise < 1:
            raise ValueError("--miner.noise must be in [0, 1)")
        if not config.axon.external_ip:
            raise ValueError("--axon.external_ip is required: it is the address published on chain")
        if config.axon.external_port is None:
            config.axon.external_port = config.axon.port


def add_args(cls, parser: argparse.ArgumentParser) -> None:
    """Arguments shared by miners and validators."""
    parser.add_argument("--netuid", type=int, help="Subnet netuid.", default=1)
    parser.add_argument("--wallet.name", type=str, help="Coldkey wallet name.", default="default")
    parser.add_argument("--wallet.hotkey", type=str, help="Hotkey name within the wallet.", default="default")
    parser.add_argument("--wallet.path", type=str, help="Wallet directory.", default="~/.bittensor/wallets")
    parser.add_argument(
        "--subtensor.network",
        type=str,
        help="finney, test, local, or a ws:// endpoint.",
        default="finney",
    )
    parser.add_argument(
        "--logging.level",
        type=str.upper,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level.",
        default="INFO",
    )
    parser.add_argument(
        "--neuron.state_dir",
        type=str,
        help="Root directory for neuron state.",
        default="~/.bittensor/miners",
    )
    parser.add_argument(
        "--neuron.epoch_length",
        type=int,
        help="Blocks between metagraph syncs (and weight sets, for validators).",
        default=100,
    )


def add_miner_args(cls, parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--neuron.name", type=str, help="Neuron name, used in the state path.", default="miner")
    parser.add_argument("--axon.ip", type=str, help="Interface the miner server binds to.", default="0.0.0.0")
    parser.add_argument("--axon.port", type=int, help="Port the miner server binds to.", default=8091)
    parser.add_argument(
        "--axon.external_ip",
        type=str,
        help="Public IPv4 address published on chain for validators to reach. Required.",
        default="",
    )
    parser.add_argument(
        "--axon.external_port",
        type=int,
        help="Public port published on chain. Defaults to --axon.port.",
        default=None,
    )
    parser.add_argument(
        "--blacklist.force_validator_permit",
        action="store_true",
        help="Only accept requests from hotkeys holding a validator permit.",
        default=False,
    )
    parser.add_argument(
        "--blacklist.allow_non_registered",
        action="store_true",
        help="Accept requests from hotkeys not registered on the subnet. Unsafe.",
        default=False,
    )
    parser.add_argument(
        "--miner.noise",
        type=float,
        help="Multiply each prediction by a random factor in [1 - noise, 1 + noise]. Testnet only.",
        default=0.0,
    )


def add_validator_args(cls, parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--neuron.name", type=str, help="Neuron name, used in the state path.", default="validator")
    parser.add_argument(
        "--neuron.window_days",
        type=int,
        help=(
            "Scoring window length in days (1 to 7). Windows start at 00:00 UTC, anchored on Monday"
            " 1970-01-05. Sent to miners in every request."
        ),
        default=7,
    )
    parser.add_argument(
        "--neuron.horizon_days",
        type=int,
        help="Days ahead to predict. Target is the window containing now + horizon.",
        default=7,
    )
    parser.add_argument("--neuron.batch_size", type=int, help="Refs sampled per query round.", default=50)
    parser.add_argument(
        "--neuron.error_band",
        type=float,
        help="Relative error at which a prediction scores 0. score = max(0, 1 - error / band).",
        default=0.15,
    )
    parser.add_argument(
        "--neuron.rolling_window_days",
        type=int,
        help="Trailing days of round scores averaged into weights.",
        default=28,
    )
    parser.add_argument(
        "--neuron.round_interval",
        type=int,
        help="Seconds between query rounds.",
        default=4 * 60 * 60,
    )
    parser.add_argument(
        "--neuron.cycle_interval",
        type=int,
        help="Seconds to sleep between cycles (scoring pass and weight check run every cycle).",
        default=60,
    )
    parser.add_argument(
        "--neuron.universe_ttl",
        type=int,
        help="Seconds to cache the oracle universe.",
        default=24 * 60 * 60,
    )
    parser.add_argument("--neuron.timeout", type=float, help="Per-miner HTTP timeout in seconds.", default=60.0)
    parser.add_argument(
        "--neuron.vpermit_tao_limit",
        type=int,
        help="Neurons with a validator permit and more stake than this are not queried as miners.",
        default=4096,
    )
    parser.add_argument(
        "--neuron.db_path",
        type=str,
        help="SQLite path. Defaults to <neuron.full_path>/watch_oracle.db.",
        default="",
    )
    parser.add_argument(
        "--neuron.disable_set_weights",
        action="store_true",
        help="Score but never set weights on chain.",
        default=False,
    )
