from typing import Iterable, List

import bittensor as bt

from watch_oracle.transport import MinerTarget


def is_queryable_miner(neuron: "bt.MetagraphNeuron", vpermit_tao_limit: int) -> bool:
    """A neuron is queryable if it serves an axon and is not a high-stake validator."""
    if not neuron.axon:
        return False
    if neuron.validator_permit and neuron.total_stake.amount > vpermit_tao_limit:
        return False
    return True


def get_miner_targets(
    metagraph: "bt.Metagraph", vpermit_tao_limit: int, exclude: Iterable[int] = ()
) -> List[MinerTarget]:
    excluded = set(exclude)
    return [
        MinerTarget(uid=n.uid, hotkey=n.hotkey, endpoint=n.axon)
        for n in metagraph.neurons
        if n.uid not in excluded and is_queryable_miner(n, vpermit_tao_limit)
    ]
